"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";

import { ChevronDownIcon, GripIcon, TrashIcon } from "@/components/icons";
import { WallSortControl } from "@/components/library-filter-bar";
import { PageNav } from "@/components/page-nav";
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
  rowTitle,
  rowsToPrefs,
  sortPresetsFor,
} from "@/lib/home-rows";
import { useUiPrefs } from "@/lib/ui-prefs";

/** 改动到落库的去抖：松手落一次位是一次改动；名字逐字键入也不会一字一请求。 */
const SAVE_DELAY_MS = 400;

/**
 * 一次拖拽的全部状态。起手时量一次就固定下来（`centers` / `height`），
 * 之后每次 pointermove 只更新 `dy` 与算出来的落点 `to`——拖拽期间行的位移都是
 * transform，不改布局，量到的坐标不会失效。
 */
interface DragState {
  /** 只认这一根指针，多指同时按不会互相打架 */
  pointerId: number;
  /** 起手时这一行在清单里的下标 */
  from: number;
  /** 当前落点下标；松手时 from !== to 才真的换位 */
  to: number;
  /** 指针相对起手点的纵向位移，直接喂给 translateY */
  dy: number;
  startY: number;
  /** 被拖那一行的高度，也是其余行让位时平移的距离 */
  height: number;
  /** 起手瞬间每一行的中线（视口坐标） */
  centers: number[];
}

/**
 * 自定义媒体库首页（/library/customize）。
 *
 * 首页 = 一份有序的行清单，每一行 = 来源 × 排序 × 名字
 * （docs/design/library-home-perspective.md）。本页是它唯一的配置面：首页上没有任何
 * 排序细节与行菜单，调整全部收在这里。
 *
 * - **独立页面，不是抽屉**：桌面与移动端同一份实现；纯列表、不放海报，效果回首页看。
 * - **没有保存键**：每次改动即时落库（去抖），只影响自己的首页（成员各存各的）。
 * - **收起时只有名字和显隐**：排序这类信息不露。点一行才展开一条紧凑的设置行——
 *   排序下拉框、「未看」开关与名字（库行）、删除（自加行）。
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
  const remove = (id: string) => {
    setExpanded((current) => (current === id ? null : current));
    commit(rowsRef.current.filter((row) => row.id !== id));
  };
  // 新加的行自动展开：它的排序与名字大概率马上要改
  const add = (row: HomeRow) => {
    commit([...rowsRef.current, row]);
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

  // —— 拖拽排序：指针事件而不是 HTML5 DnD——后者触屏根本不触发，而手机上没有别的换位入口。
  const listRef = useRef<HTMLUListElement>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  // 拖拽状态的真身放 ref：pointermove 与 pointerup 可能落在同一帧，靠 state 闭包
  // 读到的会是上一次的 to，松手时就落错位置
  const dragRef = useRef<DragState | null>(null);
  const setDragState = useCallback((next: DragState | null) => {
    dragRef.current = next;
    setDrag(next);
  }, []);
  /** 拖拽期间挂在 window 上那几个监听的卸下函数；结束、重入或组件卸载时都要调到 */
  const endDrag = useRef<(() => void) | null>(null);
  useEffect(() => () => endDrag.current?.(), []);

  /**
   * 按下把手即「把这一行拿起来」。
   *
   * **拖拽过程中不动数组**，只做位移预览：被拖的那行跟着指针 translateY 并微微抬起，
   * 让位的行整体平移一个行高（带过渡），松手才 commit 一次。这一版之前是「跨过一条
   * 中线就重排一次」，功能上没错，但手感是列表在原地跳，没有「拿起来、挪过去、放下」
   * 这回事——而且 React 一重排就把被拖的 `<li>` insertBefore 到新位置，浏览器会以
   * 「捕获目标不再连接在文档上」为由收回指针捕获，拖拽刚挪一位就断。两个毛病同一个根：
   * 拖的时候不该动真实顺序。做法与 `search-settings.tsx` 的拖拽一致，那是本仓库里
   * 先跑通的一份，抬起行的皮肤也照它。
   *
   * 起手时一次性量下所有行的中线与被拖行的高度（`centers` / `height`），之后全靠算，
   * 不再读 DOM：拖拽期间的位移是 transform，不影响布局，量一次就够，也省掉每次
   * pointermove 的强制重排。
   *
   * move / up / touchmove **同步**挂到 window 上，不走 setPointerCapture、也不走
   * useEffect，两件事都是踩出来的：
   *
   * 1. window 上的监听与「捕获被收回」无关。现在拖拽已经不重排 DOM，捕获其实也保得住，
   *    但下面第 2 条无论如何都要一个 window 级监听，一套机制比两套省事。
   * 2. **滚动要靠 touchmove 上的 preventDefault 拦，光有 touch-action 不够。** 把手上
   *    有 `touch-none`，桌面和部分安卓够用，iOS 上不够。而且必须赶在**第一条**
   *    touchmove 之前挂好——WebKit 一旦把这次手势判成滚动就不再回头，后面再
   *    preventDefault 也没用；走 useEffect 要等一次渲染，正好会漏掉第一条。
   *    `passive: false` 同样是硬要求，浏览器默认把 window 上的 touchmove 当被动监听，
   *    被动监听里的 preventDefault 直接无效。
   */
  const onGripPointerDown = (
    e: React.PointerEvent<HTMLElement>,
    id: string,
  ) => {
    e.preventDefault();
    endDrag.current?.();
    const list = listRef.current;
    if (!list) return;
    const items = Array.from(
      list.querySelectorAll<HTMLLIElement>("li[data-row-id]"),
    );
    const from = items.findIndex((li) => li.dataset.rowId === id);
    if (from === -1) return;
    const boxes = items.map((li) => li.getBoundingClientRect());
    const pointerId = e.pointerId;
    setDragState({
      pointerId,
      from,
      to: from,
      dy: 0,
      startY: e.clientY,
      height: boxes[from].height,
      centers: boxes.map((box) => box.top + box.height / 2),
    });

    const onMove = (ev: PointerEvent) => {
      const d = dragRef.current;
      if (!d || d.pointerId !== ev.pointerId) return;
      const dy = ev.clientY - d.startY;
      // 落点 = 被拖行的中线飘到哪儿之后，还有几条别的行的中线在它上面
      const center = d.centers[d.from] + dy;
      let to = 0;
      for (let i = 0; i < d.centers.length; i += 1) {
        if (i !== d.from && d.centers[i] < center) to += 1;
      }
      setDragState({ ...d, dy, to });
    };
    // 滚动已经起步之后 touchmove 会变成不可取消，这时再 preventDefault 只会招一条警告
    const onTouchMove = (ev: TouchEvent) => {
      if (ev.cancelable) ev.preventDefault();
    };
    const onEnd = (ev: PointerEvent) => {
      if (dragRef.current?.pointerId !== ev.pointerId) return;
      const d = dragRef.current;
      cleanup();
      if (d.from !== d.to) commit(moveRowTo(rowsRef.current, d.from, d.to));
    };
    function cleanup() {
      endDrag.current = null;
      setDragState(null);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      window.removeEventListener("touchmove", onTouchMove);
    }

    endDrag.current = cleanup;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
    window.addEventListener("touchmove", onTouchMove, { passive: false });
  };
  const moveByKey = (id: string, offset: -1 | 1) => {
    const from = rowsRef.current.findIndex((row) => row.id === id);
    commit(moveRowTo(rowsRef.current, from, from + offset));
  };

  /**
   * 拖拽中每一行的内联样式：被拖的那行跟着指针走并抬起（不加过渡，过渡会让它拖在
   * 手指后面），其余被跨过的行整体让位一个行高（加过渡，让位才是「滑开」而不是闪现）。
   */
  const rowStyle = (index: number): React.CSSProperties => {
    if (!drag) return {};
    if (index === drag.from) {
      return {
        transform: `translateY(${drag.dy}px) scale(1.015)`,
        position: "relative",
        zIndex: 20,
      };
    }
    const { from, to, height } = drag;
    let shift = 0;
    if (from < to && index > from && index <= to) shift = -height;
    if (to < from && index >= to && index < from) shift = height;
    return {
      transform: `translateY(${shift}px)`,
      transition: "transform 200ms ease",
    };
  };

  const visibleLibraries = (libraries ?? []).filter(
    (library) => library.viewer_access,
  );
  // 候选只收**用户自建**的合集（kind=user，手动名单与存好的筛选都算）。另两档都排除：
  // builtin 是「我的收藏」，首页已经有那一行；series 是刮削器按作品系列自动生成的
  // （「玩具总动员（系列）」那种），往往只有一两部，钉到首页连一行都滚不满，数量上
  // 却能把候选区整个淹掉——刮削的副产品不该和用户亲手攒的名单抢同一个位置。
  //
  // 只拦「添加」这一步，不改已存的行：偏好里存过的 series 合集照常渲染（合并走
  // buildHomeRows，用的是完整合集表）。真想把某个系列放上首页，合集页的 ⋯ 菜单里
  // 「显示在首页」那条路没堵，只是不再摆在这里让人一个个翻。
  const pickableCollections = collections.filter(
    (collection) => collection.kind === "user",
  );
  const collectionsOnHome = new Set(
    rows
      .filter((row) => row.kind === "collection")
      .map((row) => row.collection.id),
  );
  const shownCount = rows.filter((row) => !row.hidden).length;

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* 全站子页面统一的顶栏（components/page-nav.tsx）：常驻一颗圆形返回键、滚过页头
          后渐显吸顶小标题，并向外壳认领移动端顶栏——否则全局的 ☰ + 字标会和页内的返回
          入口摞成两条。它必须是滚动容器的**直接**子节点，sticky 才有定位参照，所以排在
          下面那层居中窄栏之外。「恢复默认」按全站约定留在标题行右端（同媒体库管理页的
          「创建媒体库」），顶栏只放返回与标题。 */}
      <PageNav
        title="自定义首页"
        fallback={{ label: "媒体库", href: "/library" as Route }}
      />
      <div className="mx-auto w-full max-w-[680px] px-6 pt-3 max-md:px-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-on-image text-[24px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
              自定义首页
            </h2>
            {/* 只报状态，不教操作：把手长得就是能拖的样子，可展开的行尾随一枚 ⌄，
                affordance 自己会说话，再写一句「拖动把手调整顺序」是把界面已经
                表达清楚的事又说了一遍 */}
            <p className="text-on-image mt-1 text-sub text-[var(--text-muted)]">
              {libraries === null
                ? "正在读取…"
                : `${shownCount} 行显示 · ${rows.length - shownCount} 行隐藏`}
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

        {/* 库与合集没回来之前不画列表：先画三条内置行、再蹦出库行，看着像列表在抖。
            这个 <ul> **不能有 overflow-hidden**：抬起的那一行要能带着圆角与投影探出
            列表边界，裁掉就成了「在盒子里蹭」而不是「被拿起来」；圆角处略微露白是这笔
            交换的代价。divide-y 常驻：拖动中若抽掉分隔线，整列会跳 1px×行数，起手瞬间
            坐标就漂了。select-none 防止拖的时候顺手把行名选成一片高亮。 */}
        {libraries !== null && (
          <ul
            ref={listRef}
            className="mt-4 select-none divide-y divide-white/[0.07] rounded-2xl border border-white/[0.09] bg-white/[0.03]"
            data-testid="home-rows"
          >
            {rows.map((row, index) => (
              <RowItem
                key={row.id}
                row={row}
                dragging={drag?.from === index}
                style={rowStyle(index)}
                expanded={expanded === row.id}
                onToggle={() =>
                  setExpanded((current) => (current === row.id ? null : row.id))
                }
                onChange={(patch) => update(row.id, patch)}
                onRemove={() => remove(row.id)}
                onGripPointerDown={(e) => onGripPointerDown(e, row.id)}
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
          {/* 库与合集同属「从哪来」的候选，必须待在同一个 flex-wrap 里：拆成两个容器时
              gap 只在各自容器内生效，接缝那一行的行距是 0，两排胶囊的描边会贴死在一起
              （看着就像叠了）。换行后的行距也要够 rounded-full 喘气，1.5 太挤。 */}
          <div className="mt-2 flex flex-wrap gap-2">
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
        </div>
        <p className="mt-4 text-caption text-[var(--text-faint)]">
          改动即时生效，只影响你自己的首页；效果回首页看。
        </p>
      </div>
    </div>
  );
}

/**
 * 这一行可选的排序，交给 WallSortControl 画成菜单：每个有方向的指标给**两条**
 * （自然方向在前，「最近添加」「最早添加」），随机 / 未看优先各一条。点一条就同时定了
 * 档位和方向，一步到位。键与海报墙偏好同一种记法：`rating` / `rating:rev`。
 * 内置的「接下来继续」「我的媒体库」没有可排的，返回 null。
 */
function sortOptions(
  row: HomeRow,
): readonly (readonly [string, string])[] | null {
  const both = (
    key: string,
    natural: string,
    reversed: string | null,
  ): (readonly [string, string])[] =>
    reversed === null
      ? [[key, natural]]
      : [
          [key, natural],
          [`${key}:rev`, reversed],
        ];
  switch (row.kind) {
    case "favorites":
      return (Object.keys(FAVORITES_SORT_PRESETS) as FavoritesSort[]).flatMap(
        (key) => {
          const preset = FAVORITES_SORT_PRESETS[key];
          return both(
            key,
            preset.name(false),
            preset.direction ? preset.name(true) : null,
          );
        },
      );
    case "collection":
    case "library": {
      const keys =
        row.kind === "library"
          ? sortPresetsFor(row.library.kind)
          : COLLECTION_SORTS;
      return keys.flatMap((key) => {
        const preset = SORT_PRESETS[key];
        return both(
          key,
          preset.short(false),
          preset.direction ? preset.short(true) : null,
        );
      });
    }
    default:
      return null;
  }
}

/**
 * 一行。收起时：把手 · 名字 · 眼睛，仅此而已（排序这类信息不露）。
 * 点名字展开一条紧凑的设置行：排序下拉框 · 未看（库行）· 名字（库行）· 删除（自加行）。
 * 内置的「接下来继续」「我的媒体库」没有可设的，点了不展开。
 */
function RowItem({
  row,
  dragging,
  style,
  expanded,
  onToggle,
  onChange,
  onRemove,
  onGripPointerDown,
  onMoveKey,
}: {
  row: HomeRow;
  /** 正被拿在手里的那一行：换成抬起的皮肤 */
  dragging: boolean;
  /** 拖拽期间的位移（被拖行跟手、其余行让位），由页面的 rowStyle 算好 */
  style: React.CSSProperties;
  expanded: boolean;
  onToggle: () => void;
  onChange: (patch: (row: HomeRow) => HomeRow) => void;
  onRemove: () => void;
  /** 按下即进入拖拽；后续的 move / up 由页面挂在 window 上接管（见上面的注释） */
  onGripPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onMoveKey: (offset: -1 | 1) => void;
}) {
  const title = rowTitle(row);
  const options = sortOptions(row);
  const editable = options !== null;
  const sort =
    row.kind === "favorites" ||
    row.kind === "library" ||
    row.kind === "collection"
      ? row.sort
      : null;
  const removable =
    row.kind === "collection" || (row.kind === "library" && !row.builtin);
  const reversed =
    row.kind === "favorites" ||
    row.kind === "library" ||
    row.kind === "collection"
      ? row.reversed
      : false;
  // 「只显示我没看过的」只对库行有意义，且与「最近观看」互斥（那一行只要播过的）
  const showUnwatched = row.kind === "library" && row.sort !== "last_played";
  const nameInputId = `row-name-${row.id}`;
  // 能自己起名字的行：库行与合集行。留空各自跟随默认，占位就写出那个默认值是什么，
  // 顺带也把这一行的**来源**说清楚——名字改掉之后，占位是认出它指向哪个库/哪个合集
  // 的唯一线索。内置的三行没有来源可言，不给改名。
  const naming =
    row.kind === "library"
      ? {
          value: row.name,
          placeholder: SORT_PRESETS[row.sort].name(
            row.library.name,
            row.reversed,
          ),
          hint: "留空跟随排序推荐",
        }
      : row.kind === "collection"
        ? {
            value: row.name,
            placeholder: row.collection.name,
            hint: "留空用合集自己的名字",
          }
        : null;
  const open = expanded && editable;
  return (
    <li
      data-row-id={row.id}
      data-testid={`home-row-item-${row.id}`}
      data-hidden={row.hidden ? "true" : undefined}
      style={style}
      /* 抬起的皮肤照抄 search-settings 的拖拽行：几乎不透的底 + 一圈细描边 + 一片
         往下打的投影，再加自己的圆角，让它看着是**离开列表被捏在手里**的一张卡。
         底必须够实——下面的行正从它身下滑过，半透的底会把两行糊在一起。
         这一行不加 transition：跟手要 1:1，加了过渡就拖在手指后面。 */
      className={
        dragging
          ? "rounded-xl bg-[#1d222d]/95 shadow-[0_16px_40px_-12px_rgba(0,0,0,0.75)] ring-1 ring-white/[0.16] backdrop-blur-md"
          : `transition ${open ? "bg-white/[0.045]" : ""}`
      }
    >
      <div className="flex h-12 items-center gap-1 pl-2 pr-2">
        {/* 把手：唯一的换位入口。
            `touch-none` 只是第一道闸，触屏上真正拦住页面滚动的是拖拽期间那条
            非被动的 touchmove 监听（见上面 onGripPointerDown 的注释）。
            移动端放大到 44px：iOS HIG 的最小可点目标，与顶栏圆键同一档
            （page-nav.tsx）。32px 的把手在触屏上很容易按空，按空就落到行上，
            手指一滑页面就滚起来——「拖不动 + 页面在滚」有一半是这么来的。
            行高 48px，44px 正好塞得下。 */}
        <button
          type="button"
          aria-label={`拖动调整「${title}」的顺序（或按 Alt + 上下方向键）`}
          title="拖动调整顺序"
          onPointerDown={onGripPointerDown}
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
          className="grid size-8 shrink-0 cursor-grab touch-none place-items-center rounded-md text-white/25 outline-none transition hover:bg-white/[0.06] hover:text-white/70 focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] active:cursor-grabbing max-md:size-11"
          data-testid="row-grip"
        >
          <GripIcon className="size-4" />
        </button>
        {/* 名字：可设的行点它展开设置；不可设的行只是文字。
            收起态整行只剩一个名字，「还能点开」得由行自己说出来：可设的行尾随一枚
            随展开翻转的 ⌄ 并给 hover 底色，不可设的行两样都不画——一眼就能分出
            哪几行点得开，也不会让用户去点一个点了没反应的行。 */}
        <button
          type="button"
          disabled={!editable}
          aria-expanded={editable ? open : undefined}
          onClick={editable ? onToggle : undefined}
          className={`flex h-full min-w-0 flex-1 items-center gap-2 rounded-md px-1.5 text-left text-ui text-[var(--text)] outline-none transition focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:cursor-default ${
            editable ? "hover:bg-white/[0.06]" : ""
          } ${row.hidden ? "text-[var(--text-faint)]" : ""}`}
          data-testid="row-title"
        >
          <span className="truncate">{title}</span>
          {row.hidden && (
            <span className="shrink-0 text-caption text-[var(--text-faint)]">
              已隐藏
            </span>
          )}
          {editable && (
            <ChevronDownIcon
              className={`size-3.5 shrink-0 text-[var(--text-faint)] transition-transform ${
                open ? "rotate-180" : ""
              }`}
              data-testid="row-disclosure"
            />
          )}
        </button>
        <button
          type="button"
          aria-label={row.hidden ? `显示「${title}」` : `隐藏「${title}」`}
          aria-pressed={!row.hidden}
          title={row.hidden ? "显示这一行" : "隐藏这一行"}
          onClick={() => onChange((r) => ({ ...r, hidden: !r.hidden }))}
          className={`grid size-8 shrink-0 place-items-center rounded-md transition hover:bg-white/[0.08] hover:text-[var(--text)] ${
            row.hidden ? "text-[var(--text-faint)]" : "text-[var(--text-muted)]"
          }`}
          data-testid="row-visibility"
        >
          <EyeIcon off={row.hidden} />
        </button>
      </div>

      {open && options && sort !== null && (
        /* 两列网格：左列是「排序」「名字」这两个标签，右列是控件，标签因此上下对齐；
           开关与删除放最后一行、横跨两列，左右各一头。原先是一整行 flex-wrap，
           几个控件挤在一起、换行之后参差不齐 */
        <div
          className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2.5 px-3 pb-3 pl-[46px] max-md:pl-3"
          data-testid="row-settings"
        >
          {/* 排序：一个控件同时定档位和方向——菜单里每个指标两条（「最近添加」
              「最早添加」），点一条就是一步到位；控件复用海报墙的排序菜单，同一个东西
              站内一副长相。早先是 <select> 选指标 + 一颗方向按钮：iOS 上原生选择器
              收起的那一下会吞掉紧接着的点击，方向按钮总要点两次，两个控件也对不齐 */}
          <span className="text-sub text-[var(--text-muted)]">排序</span>
          <div className="min-w-0" data-testid="row-sort">
            <WallSortControl
              value={reversed ? `${sort}:rev` : sort}
              options={options}
              onChange={(next) => {
                const [key, flag] = next.split(":");
                const nextReversed = flag === "rev";
                onChange((r) => {
                  if (r.kind === "favorites")
                    return {
                      ...r,
                      sort: key as FavoritesSort,
                      reversed: nextReversed,
                    };
                  if (r.kind === "collection")
                    return {
                      ...r,
                      sort: key as HomeRowSort,
                      reversed: nextReversed,
                    };
                  if (r.kind === "library") {
                    const sort = key as HomeRowSort;
                    // 「最近观看」只要播过的，与「只看没看过的」互斥
                    return {
                      ...r,
                      sort,
                      reversed: nextReversed,
                      unwatched: sort === "last_played" ? false : r.unwatched,
                    };
                  }
                  return r;
                });
              }}
            />
          </div>
          {naming && (
            <>
              <label
                htmlFor={nameInputId}
                className="text-sub text-[var(--text-muted)]"
              >
                名字
              </label>
              <input
                id={nameInputId}
                value={naming.value}
                placeholder={naming.placeholder}
                maxLength={40}
                aria-label={`「${title}」的名字`}
                title={naming.hint}
                onChange={(e) => {
                  const name = e.target.value;
                  onChange((r) =>
                    r.kind === "library" || r.kind === "collection"
                      ? { ...r, name }
                      : r,
                  );
                }}
                onBlur={() =>
                  onChange((r) =>
                    r.kind === "library" || r.kind === "collection"
                      ? { ...r, name: r.name.trim() }
                      : r,
                  )
                }
                className="h-8 min-w-0 rounded-md border border-white/15 bg-white/[0.05] px-2 text-sub text-[var(--text)] outline-none placeholder:text-[var(--text-faint)] focus:border-white/30"
                data-testid="row-name"
              />
            </>
          )}
          {(showUnwatched || removable) && (
            <div className="col-span-2 flex items-center justify-between gap-3">
              {showUnwatched && row.kind === "library" ? (
                <label className="flex items-center gap-2 text-sub text-[var(--text-muted)]">
                  <input
                    type="checkbox"
                    checked={row.unwatched}
                    onChange={(e) => {
                      const unwatched = e.target.checked;
                      onChange((r) =>
                        r.kind === "library" ? { ...r, unwatched } : r,
                      );
                    }}
                    className="size-4 accent-[#7fb0ff]"
                    data-testid="row-unwatched"
                  />
                  只显示我没看过的
                </label>
              ) : (
                <span />
              )}
              {removable && (
                <button
                  type="button"
                  onClick={onRemove}
                  className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-sub text-[var(--text-muted)] transition hover:bg-white/[0.06] hover:text-[var(--danger)]"
                  data-testid="row-remove"
                >
                  <TrashIcon className="size-3.5" />
                  删除这一行
                </button>
              )}
            </div>
          )}
        </div>
      )}
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
