"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useConfirm } from "@/components/feedback";
import { SearchIcon } from "@/components/icons";
import {
  clearSearchHistory,
  deleteSearchHistoryEntry,
  listSearchHistory,
  type SearchHistoryItem,
} from "@/lib/api/search";
import {
  CATEGORY_LABEL,
  SCOPE_ALL,
  scopeOfTab,
  type SearchScope,
  type SearchTab,
  type SearchVertical,
} from "@/lib/categories";
import { useSearchAccess } from "@/lib/search-access";
import { useSearchPrefs } from "@/lib/search-prefs";
import { formatRelativeTime } from "@/lib/time";

/**
 * 侧栏搜索入口 + 命令面板（Command Palette）。
 *
 * 全站唯一搜索入口，三模式（媒体优先）：触发器是品牌行右侧的放大镜图标按钮，
 * 点击（或 ⌘K）弹出面板。视觉对齐 Raycast/Spotlight 的**实心深色浮层**——
 * 纯 CSS 面板（不再用 WebGL 液态玻璃：弹窗是高频工具，要的是安静、快、可读，
 * 折射效果在这里只会增加视觉噪音），结构自上而下三段：
 *   输入行   放大镜 + 关键词输入 + 「影视 | 资源 | 媒体库」分段（右侧，Tab 键可切）
 *   主体     资源模式先出一行分类 chips；下方是最近搜索（媒体/资源混排，
 *            图标 + 类型徽标区分，↑↓ 可选、输入即过滤、hover 可删）
 *   页脚     左侧当前模式说明，右侧快捷键提示
 *
 * 提交与回放都走 onSearch：关键词 + 范围 + { vertical, snapshotId }，由上层
 * 编码进 /search 的 URL。历史点击按记录自身的垂直回放（媒体历史带快照 id
 * 进快照预览，资源历史沿用原有快照/重搜逻辑）。
 */

/** 提交搜索的附加选项：目标垂直 + 历史快照回放。 */
export interface SearchSubmitOptions {
  /** 落地垂直；缺省 = 站点资源（torrent），与老调用方行为一致 */
  vertical?: SearchVertical;
  /** 非空 = 预览该条历史的结果快照（点历史记录进入），而非发起实时搜索 */
  snapshotId?: number;
}

export interface SearchCommandProps {
  /** 提交搜索的回调：关键词 + 搜索范围（标签换算而来）。由上层负责落地展示。 */
  onSearch: (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => void;
  /**
   * 触发键的样式覆盖。缺省是侧栏/顶栏那副玻璃行图标键；放进别处的控件行时
   * （如详情页顶栏与 ⋯ 并排，见 components/page-nav.tsx）传入该行自己的按钮
   * 类名，否则一颗方玻璃挨着一颗圆键，会像两套控件凑在一起。
   */
  triggerClassName?: string;
}

/** 搜索面板上次停留位置的浏览器级记忆，不随账号同步。 */
const SEARCH_PALETTE_STATE_KEY = "movieclaw.search-palette-state";

interface SearchPaletteState {
  mode: SearchVertical;
  tabKey: string;
}

/**
 * 从 localStorage 恢复搜索模式和资源分类。
 * 存储内容可能来自旧版本或被手动修改，因此只接受当前认识的字段和值。
 */
function readSearchPaletteState(): SearchPaletteState {
  const fallback: SearchPaletteState = { mode: "media", tabKey: "all" };
  try {
    const value = JSON.parse(localStorage.getItem(SEARCH_PALETTE_STATE_KEY) ?? "null") as
      | Partial<SearchPaletteState>
      | null;
    return {
      mode:
        value?.mode === "torrent" || value?.mode === "media" || value?.mode === "library"
          ? value.mode
          : fallback.mode,
      tabKey:
        typeof value?.tabKey === "string" && value.tabKey.length > 0
          ? value.tabKey
          : fallback.tabKey,
    };
  } catch {
    return fallback;
  }
}

/** localStorage 不可用时仅失去记忆能力，不影响搜索本身。 */
function writeSearchPaletteState(state: SearchPaletteState): void {
  try {
    localStorage.setItem(SEARCH_PALETTE_STATE_KEY, JSON.stringify(state));
  } catch {
    // 隐私模式或存储空间不足时静默降级
  }
}

/** 标签在分类 chips 里的选中态 key：类型加前缀，内置分类与预设的 id 不会互相撞。 */
function tabKeyOf(tab: SearchTab): string {
  return `${tab.type}:${tab.id}`;
}

/** 同关键词下的媒体搜索与各资源范围记录，默认在历史列表里折叠成一行。 */
interface HistoryGroup {
  /** 忽略首尾空格和英文大小写后的分组键。 */
  key: string;
  /** 使用组内最近一条记录的原文展示关键词。 */
  keyword: string;
  /** 组内按最近搜索时间倒序，首条即主行回车时打开的记录。 */
  items: SearchHistoryItem[];
}

/** 把接口返回的有序记录折叠为关键词组，保持后端给出的组顺序与组内顺序。 */
function groupHistory(items: SearchHistoryItem[]): HistoryGroup[] {
  const groups = new Map<string, HistoryGroup>();
  for (const item of items) {
    const key = item.keyword.trim().toLocaleLowerCase();
    const group = groups.get(key);
    if (group) group.items.push(item);
    else groups.set(key, { key, keyword: item.keyword, items: [item] });
  }
  return [...groups.values()];
}

export function SearchCommand({
  onSearch,
  // 移动端 44px：iOS HIG 最小可点目标（触屏），桌面保持 32px 紧凑图标键
  triggerClassName = "glass-row !size-8 shrink-0 justify-center !p-0 max-md:!size-11",
}: SearchCommandProps) {
  const searchAccess = useSearchAccess();
  const [open, setOpen] = useState(false);
  // 面板是全屏浮层，Portal 到 body：避免被 sidebar 玻璃面板的 isolation:isolate
  // 层叠上下文困住。portalReady 规避 SSR。
  const [portalReady, setPortalReady] = useState(false);
  useEffect(() => setPortalReady(true), []);

  // 面板打开时给 body 挂 cmdk-open 类，驱动 .app-shell 轻微缩放后推（Spotlight 式纵深）
  useEffect(() => {
    document.body.classList.toggle("cmdk-open", open);
    return () => document.body.classList.remove("cmdk-open");
  }, [open]);

  // ⌘K / Ctrl+K 全局唤起；已打开时再次按下则关闭
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  if (searchAccess.ready && searchAccess.available.length === 0) return null;

  return (
    <>
      {/* 触发器保持紧凑；打开后的输入提示会告诉用户下次可直接用快捷键唤起。 */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="搜索（⌘K 或 Ctrl+K）"
        aria-haspopup="dialog"
        title="搜索（⌘K 或 Ctrl+K）"
        className={triggerClassName}
      >
        <SearchIcon className="size-[18px] max-md:size-[22px]" />
      </button>

      {/* 面板按需挂载：每次打开都是全新实例，状态（模式/分类/输入）自然重置，
          不需要「打开时逐项复位」的清理逻辑；关闭即时卸载（对齐 Raycast 的干脆手感） */}
      {portalReady &&
        open &&
        createPortal(
          <SearchPalette
            onClose={() => setOpen(false)}
            onSearch={(keyword, scope, options) => {
              onSearch(keyword, scope, options);
              setOpen(false);
            }}
          />,
          document.body,
        )}
    </>
  );
}

/* —— 面板本体 —— */

function SearchPalette({
  onClose,
  onSearch,
}: {
  onClose: () => void;
  onSearch: (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => void;
}) {
  const { visibleTabs, loading: tabsLoading } = useSearchPrefs();
  const searchAccess = useSearchAccess();
  const confirm = useConfirm();
  const [keyword, setKeyword] = useState("");
  // 面板每次重新挂载，但模式与资源分类从浏览器级记忆恢复。
  const [rememberedState] = useState(readSearchPaletteState);
  const [mode, setMode] = useState<SearchVertical>(rememberedState.mode);
  // 搜资源模式选中的分类 key；"all" = 全部。
  const [tabKey, setTabKey] = useState(rememberedState.tabKey);
  // null = 加载中；[] = 无历史
  const [items, setItems] = useState<SearchHistoryItem[] | null>(null);
  // 关键词组默认全部展开；这里只记录用户在本次弹窗里主动收起的组。
  // 输入过滤时匹配组会临时强制展开，但不会抹掉用户的收起选择。
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set());
  // 键盘高亮的关键词组下标；-1 = 未选中（此时回车提交输入的关键词）
  const [sel, setSel] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const availableModes = searchAccess.available;

  // 挂载即聚焦输入框：effect 执行时 DOM 已提交，直接同步 focus（不要用 rAF，
  // 后台标签页里 rAF 会被挂起导致聚焦丢失）
  useEffect(() => inputRef.current?.focus(), []);

  useEffect(() => {
    if (!searchAccess.ready) return;
    if (availableModes.length === 0) {
      onClose();
      return;
    }
    if (!availableModes.includes(mode)) {
      const nextMode = searchAccess.firstAvailable ?? availableModes[0];
      setMode(nextMode);
      writeSearchPaletteState({ mode: nextMode, tabKey });
    }
  }, [availableModes, mode, onClose, searchAccess.firstAvailable, searchAccess.ready, tabKey]);

  // 等服务端搜索偏好加载完成后再校验。若上次分类已隐藏或删除，回退到「全部」，
  // 避免界面没有选中项但提交时悄悄按全部搜索。
  useEffect(() => {
    if (
      !tabsLoading &&
      tabKey !== "all" &&
      !visibleTabs.some((tab) => tabKeyOf(tab) === tabKey)
    ) {
      setTabKey("all");
      writeSearchPaletteState({ mode, tabKey: "all" });
    }
  }, [mode, tabKey, tabsLoading, visibleTabs]);

  const changeMode = (nextMode: SearchVertical) => {
    if (!availableModes.includes(nextMode)) return;
    setMode(nextMode);
    writeSearchPaletteState({ mode: nextMode, tabKey });
  };

  const changeTab = (nextTabKey: string) => {
    setTabKey(nextTabKey);
    writeSearchPaletteState({ mode, tabKey: nextTabKey });
  };

  // 历史存在后端（search_history 表）；limit 按关键词组计算，组内范围会完整返回。
  useEffect(() => {
    let cancelled = false;
    listSearchHistory(8)
      .then((list) => !cancelled && setItems(list))
      .catch(() => !cancelled && setItems([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const groups = useMemo(
    () =>
      groupHistory(
        (items ?? []).filter((item) =>
          item.vertical === "titles" ? searchAccess.canMedia : searchAccess.canTorrent,
        ),
      ),
    [items, searchAccess.canMedia, searchAccess.canTorrent],
  );

  // 输入即过滤关键词组（子串匹配）；匹配后 UI 自动展开该组的所有具体范围。
  const filteredGroups = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (!needle) return groups;
    return groups.filter((group) => group.key.includes(needle));
  }, [groups, keyword]);

  // 过滤结果变化后旧下标可能越界，收敛到组列表末尾；空列表回到 -1。
  useEffect(() => {
    setSel((prev) => Math.min(prev, filteredGroups.length - 1));
  }, [filteredGroups.length]);

  const submit = () => {
    const kw = keyword.trim();
    if (!availableModes.includes(mode)) return;
    if (mode === "torrent") {
      // 资源模式允许空关键词 = 浏览：直接逛选中分类的站点种子列表页，
      // 对应"我不想搜某一部，就想看看这个分类最近出了什么"
      const tab = visibleTabs.find((t) => tabKeyOf(t) === tabKey);
      onSearch(kw, tab ? scopeOfTab(tab) : SCOPE_ALL, { vertical: "torrent" });
      return;
    }
    // 影视/媒体库没有「浏览」语义（无分类维度），空词不提交，范围恒为「全部」
    if (!kw) return;
    onSearch(kw, SCOPE_ALL, { vertical: mode });
  };

  /** 点开一条历史：按记录自身的垂直回放，有快照进快照预览，没有发起实时搜索。 */
  const pick = (item: SearchHistoryItem) => {
    const snapshotId = item.has_snapshot ? item.id : undefined;
    if (item.vertical === "titles") {
      onSearch(item.keyword, SCOPE_ALL, { vertical: "media", snapshotId });
      return;
    }
    onSearch(
      item.keyword,
      {
        label: item.label,
        categories: item.categories,
        siteIds: item.site_ids,
        // 还原发起搜索时的图览模式；skipHistory 恒 false——能出现在历史里的
        // 搜索本来就不是无痕的，点它重搜也照常记录
        posterMode: item.poster_mode,
        skipHistory: false,
      },
      { vertical: "torrent", snapshotId },
    );
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        onClose();
        break;
      // Tab 在三种模式间轮换：面板是模态浮层，焦点常驻输入框，Tab 的原生
      // 焦点移动在这里没有意义，挪用作模式切换（与页脚提示文案呼应）
      case "Tab":
        event.preventDefault();
        if (availableModes.length > 1) {
          const index = availableModes.indexOf(mode);
          changeMode(availableModes[(index + 1) % availableModes.length]);
        }
        break;
      case "ArrowDown":
        event.preventDefault();
        setSel((prev) => Math.min(prev + 1, filteredGroups.length - 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        setSel((prev) => Math.max(prev - 1, -1));
        break;
      case "ArrowRight": {
        const group = filteredGroups[sel];
        if (!group || group.items.length === 1) break;
        event.preventDefault();
        setCollapsedGroups((prev) => {
          const next = new Set(prev);
          next.delete(group.key);
          return next;
        });
        break;
      }
      case "ArrowLeft": {
        const group = filteredGroups[sel];
        if (!group || group.items.length === 1) break;
        event.preventDefault();
        setCollapsedGroups((prev) => new Set(prev).add(group.key));
        break;
      }
      case "Enter":
        event.preventDefault();
        if (sel >= 0 && filteredGroups[sel]) pick(filteredGroups[sel].items[0]);
        else submit();
        break;
    }
  };

  const removeOne = (id: number) => {
    setItems((prev) => (prev ? prev.filter((i) => i.id !== id) : prev));
    deleteSearchHistoryEntry(id).catch(() => undefined);
  };

  const removeGroup = async (group: HistoryGroup) => {
    if (
      group.items.length > 1 &&
      !(await confirm({
        title: `删除「${group.keyword}」的 ${group.items.length} 条搜索记录？`,
        confirmLabel: "删除",
        tone: "danger",
      }))
    ) {
      return;
    }
    const ids = new Set(group.items.map((item) => item.id));
    setItems((prev) => (prev ? prev.filter((item) => !ids.has(item.id)) : prev));
    Promise.all(group.items.map((item) => deleteSearchHistoryEntry(item.id))).catch(() => undefined);
  };

  const toggleGroup = (groupKey: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupKey)) next.delete(groupKey);
      else next.add(groupKey);
      return next;
    });
  };

  const removeAll = () => {
    setItems([]);
    clearSearchHistory().catch(() => undefined);
  };

  return (
    // 遮罩：mousedown 落在遮罩本身（而非面板内）即关闭
    <div
      // bottom 超出视口 --vp-overshoot：遮罩铺到屏幕物理底边（见 globals.css 的说明）。
      // 面板 items-start 顶部对齐，加高遮罩不会挪动它。
      className="search-palette-overlay fixed inset-0 z-[80] flex items-start justify-center px-4 pt-[13vh] [bottom:calc(-1*var(--vp-overshoot))] max-md:px-2 max-md:pt-[calc(var(--safe-top)+10px)]"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="搜索"
        className="search-palette-panel flex w-full max-w-[600px] flex-col overflow-hidden rounded-2xl border border-white/[0.09] bg-[rgba(21,23,29,0.96)] shadow-[0_24px_80px_rgba(0,0,0,0.55),0_2px_8px_rgba(0,0,0,0.4)] backdrop-blur-2xl max-md:max-h-[calc(100dvh-var(--safe-top)-var(--keyboard-inset)-20px)]"
        onKeyDown={handleKeyDown}
      >
        {/* —— 输入行 —— */}
        <div className="flex items-center gap-3 pl-4 pr-3">
          <SearchIcon className="size-[17px] shrink-0 text-[var(--text-faint)]" />
          <input
            ref={inputRef}
            value={keyword}
            onChange={(event) => {
              setKeyword(event.target.value);
              setSel(-1); // 输入变化 = 用户意图回到「搜新词」，清掉历史高亮
            }}
            placeholder={
              mode === "media"
                ? "搜索电影、剧集… · 下次按 ⌘K / Ctrl+K 唤醒"
                : mode === "torrent"
                  ? "搜索资源或 IMDb ID… · 下次按 ⌘K / Ctrl+K 唤醒"
                  : "搜索已入库的影片… · 下次按 ⌘K / Ctrl+K 唤醒"
            }
            aria-label={
              mode === "media" ? "搜索影视条目" : mode === "torrent" ? "搜索站点资源" : "搜索媒体库"
            }
            // 搜索词是片名/IMDb ID，不是英文句子：iOS 的首字母大写和自动纠错
            // 会把 "tt0111161"、"Dune" 这类输入改得面目全非，全部关掉
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            className="h-[52px] min-w-0 flex-1 bg-transparent text-body-lg text-[var(--text)] outline-none placeholder:text-[var(--text-faint)]"
          />
          <ModeSwitch mode={mode} modes={availableModes} onChange={changeMode} />
        </div>

        {/* —— 分类 chips（仅资源模式；影视/媒体库搜索没有分类维度）—— */}
        {mode === "torrent" && (
          <div className="flex flex-wrap gap-1.5 px-4 pb-3">
            <CategoryChip
              label="全部"
              active={tabKey === "all"}
              onClick={() => changeTab("all")}
            />
            {visibleTabs.map((tab) => (
              <CategoryChip
                key={tabKeyOf(tab)}
                label={tab.type === "category" ? CATEGORY_LABEL[tab.id] : tab.name}
                active={tabKey === tabKeyOf(tab)}
                onClick={() => changeTab(tabKeyOf(tab))}
              />
            ))}
          </div>
        )}

        <div className="h-px bg-white/[0.06]" />

        {/* —— 主体：最近搜索（媒体/资源混排）—— */}
        <div className="scroll-thin max-h-[336px] min-h-[96px] overflow-y-auto p-2 max-md:max-h-none max-md:min-h-0 max-md:flex-1">
          {items !== null && items.length > 0 && (
            <div className="flex items-center justify-between px-2.5 pb-1 pt-1">
              <span className="text-caption font-medium tracking-wide text-[var(--text-faint)]">
                最近搜索
              </span>
              <button
                type="button"
                onClick={removeAll}
                className="touch-target rounded-md px-1.5 py-0.5 text-caption text-[var(--text-faint)] transition-colors hover:bg-white/[0.08] hover:text-[var(--text-muted)]"
              >
                清空
              </button>
            </div>
          )}
          {items !== null && items.length === 0 && (
            <p className="px-2.5 py-6 text-center text-sub text-[var(--text-faint)]">
              还没有搜索记录，输入关键词回车开始搜索
            </p>
          )}
          {items !== null && items.length > 0 && filteredGroups.length === 0 && (
            <p className="px-2.5 py-6 text-center text-sub text-[var(--text-faint)]">
              没有匹配「{keyword.trim()}」的搜索记录，回车直接搜索
            </p>
          )}
          {filteredGroups.length > 0 && (
            <ul aria-label="最近搜索">
              {filteredGroups.map((group, index) => (
                <HistoryGroupRow
                  key={group.key}
                  group={group}
                  active={index === sel}
                  onHover={() => setSel(index)}
                  onPick={() => pick(group.items[0])}
                  expanded={keyword.trim().length > 0 || !collapsedGroups.has(group.key)}
                  onToggle={() => toggleGroup(group.key)}
                  onPickItem={pick}
                  onRemoveItem={removeOne}
                  onRemoveGroup={() => void removeGroup(group)}
                />
              ))}
            </ul>
          )}
        </div>

        {/* —— 页脚：左侧模式说明，右侧快捷键 —— */}
        <div className="flex h-10 shrink-0 items-center justify-between border-t border-white/[0.06] px-4">
          <span className="text-caption text-[var(--text-faint)]">
            {mode === "media"
              ? "在豆瓣中搜索影视条目"
              : mode === "torrent"
                ? "跨全部已配置站点搜索种子，留空回车 = 浏览该分类最新资源"
                : "在媒体库中搜索已入库的影片"}
          </span>
          <span className="flex items-center gap-3 text-caption text-[var(--text-faint)] max-md:hidden">
            <span className="flex items-center gap-1">
              <Kbd>⏎</Kbd> {mode === "torrent" && !keyword.trim() ? "浏览" : "搜索"}
            </span>
            <span className="flex items-center gap-1">
              <Kbd>Tab</Kbd> 切换范围
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↑↓</Kbd> 历史
            </span>
            <span className="flex items-center gap-1">
              <Kbd>esc</Kbd> 关闭
            </span>
          </span>
        </div>
      </div>
    </div>
  );
}

/* —— 小件 —— */

/** 「影视 | 资源 | 媒体库」分段：输入行右侧的紧凑三段开关（Raycast 的 scope 选择位）。
 *  命名按内容来源走（不带「搜」前缀，弹窗本身就是搜索场景）：影视 = 世界上有什么
 *  （豆瓣条目），资源 = 去哪儿下（跨站点种子），媒体库 = 我有没有（本地已入库）。 */
function ModeSwitch({
  mode,
  modes,
  onChange,
}: {
  mode: SearchVertical;
  modes: SearchVertical[];
  onChange: (mode: SearchVertical) => void;
}) {
  const options: { id: SearchVertical; label: string; hint: string }[] = [
    { id: "media", label: "影视", hint: "影视条目（豆瓣）" },
    { id: "torrent", label: "资源", hint: "跨站点种子搜索" },
    { id: "library", label: "媒体库", hint: "已入库的本地影片" },
  ] satisfies { id: SearchVertical; label: string; hint: string }[];
  const visibleOptions = options.filter((option) => modes.includes(option.id));
  if (visibleOptions.length <= 1) return null;
  return (
    <div
      role="radiogroup"
      aria-label="搜索范围"
      className="flex shrink-0 gap-0.5 rounded-lg bg-white/[0.06] p-0.5"
    >
      {visibleOptions.map((opt) => {
        const active = opt.id === mode;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            title={opt.hint}
            // mousedown 抢焦点会让输入框失焦，preventDefault 保持焦点常驻输入框
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => onChange(opt.id)}
            className={`rounded-md px-2.5 py-1 text-sub font-medium transition-colors ${
              active
                ? "bg-white/[0.13] text-[var(--text)]"
                : "text-[var(--text-muted)] hover:text-[var(--text)]"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/** 分类 chip（资源模式）：展示哪些标签、什么顺序由「设置 → 搜索」的偏好决定。 */
function CategoryChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      className={`rounded-full px-2.5 py-[3px] text-sub transition-colors ${
        active
          ? "bg-white/[0.14] font-medium text-[var(--text)]"
          : "text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-[var(--text)]"
      }`}
    >
      {label}
    </button>
  );
}

/**
 * 关键词组主行：默认只占一行，点击/回车打开最近一次搜索；右侧箭头按需展开
 * 媒体及各资源范围。组内只有一条时仍沿用相同结构，保持列表节奏稳定。
 */
function HistoryGroupRow({
  group,
  active,
  onHover,
  onPick,
  expanded,
  onToggle,
  onPickItem,
  onRemoveItem,
  onRemoveGroup,
}: {
  group: HistoryGroup;
  active: boolean;
  onHover: () => void;
  onPick: () => void;
  expanded: boolean;
  onToggle: () => void;
  onPickItem: (item: SearchHistoryItem) => void;
  onRemoveItem: (id: number) => void;
  onRemoveGroup: () => void;
}) {
  const latest = group.items[0];
  const mediaCount = group.items.filter((item) => item.vertical === "titles").length;
  const torrentCount = group.items.length - mediaCount;

  // 单条记录不制造「只有一个孩子的分组」：沿用旧版扁平行，点击即进入该记录。
  if (group.items.length === 1) {
    return (
      <HistorySingleRow
        item={latest}
        active={active}
        onHover={onHover}
        onPick={onPick}
        onRemove={onRemoveGroup}
      />
    );
  }

  return (
    <li className="group/history">
      <div
        className={`flex items-center rounded-[10px] transition-colors ${
          active ? "bg-white/[0.07]" : ""
        }`}
        onMouseEnter={onHover}
      >
        <button
          type="button"
          onClick={onPick}
          className="flex min-w-0 flex-1 items-center gap-2.5 py-2 pl-2.5 text-left"
        >
          <span className="min-w-0 flex-1 truncate text-ui leading-5 text-[var(--text)]/90">
            {group.keyword}
          </span>
          {group.items.length > 1 ? (
            <>
              <span className="shrink-0 rounded-md bg-white/[0.07] px-1.5 py-0.5 text-micro text-[var(--text-muted)]">
                {group.items.length} 种范围
              </span>
              <span className="hidden shrink-0 text-micro text-[var(--text-faint)] sm:inline">
                {mediaCount > 0 && `影视 ${mediaCount}`}
                {mediaCount > 0 && torrentCount > 0 && " · "}
                {torrentCount > 0 && `资源 ${torrentCount}`}
              </span>
            </>
          ) : (
            <HistoryTypeBadges item={latest} />
          )}
          {latest.has_snapshot && (
            <span
              title="最近一次搜索已有结果快照，点击秒开预览"
              className="shrink-0 rounded-md bg-[var(--info-soft)]/15 px-1.5 py-0.5 text-micro text-[var(--info-text-2)]"
            >
              快照
            </span>
          )}
          <span className="shrink-0 text-caption text-[var(--text-faint)]">
            {formatRelativeTime(latest.last_searched_at)}
          </span>
        </button>
        <button
          type="button"
          aria-label={expanded ? `收起 ${group.keyword} 的搜索范围` : `展开 ${group.keyword} 的搜索范围`}
          aria-expanded={expanded}
          onMouseDown={(event) => event.preventDefault()}
          onClick={onToggle}
          className="touch-target mx-0.5 rounded-md p-1.5 text-[var(--text-faint)] transition-colors hover:bg-white/[0.08] hover:text-[var(--text-muted)]"
        >
          <svg
            viewBox="0 0 20 20"
            className={`size-3.5 transition-transform ${expanded ? "rotate-90" : ""}`}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="m7.5 4.5 5 5-5 5" />
          </svg>
        </button>
        <DeleteHistoryButton
          label={`删除搜索历史组：${group.keyword}`}
          onClick={onRemoveGroup}
          className="touch-reveal mr-1 opacity-0 group-hover/history:opacity-100"
        />
      </div>

      {expanded && (
        <ul className="ml-[18px] border-l border-white/[0.07] py-0.5 pl-3">
          {group.items.map((item) => (
            <HistoryVariantRow
              key={item.id}
              item={item}
              onPick={() => onPickItem(item)}
              onRemove={() => onRemoveItem(item.id)}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

/**
 * 只有一条记录的关键词保持旧版扁平展示：类型、资源分类、快照和时间同层呈现，
 * 没有摘要、展开箭头或重复的子行，与多记录分组自然混排。
 */
function HistorySingleRow({
  item,
  active,
  onHover,
  onPick,
  onRemove,
}: {
  item: SearchHistoryItem;
  active: boolean;
  onHover: () => void;
  onPick: () => void;
  onRemove: () => void;
}) {
  return (
    <li className="group/single relative">
      <button
        type="button"
        onMouseEnter={onHover}
        onClick={onPick}
        className={`flex w-full items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-left transition-colors ${
          active ? "bg-white/[0.07]" : ""
        }`}
      >
        <span className="min-w-0 flex-1 truncate text-ui leading-5 text-[var(--text)]/90">
          {item.keyword}
        </span>
        <HistoryTypeBadges item={item} />
        {item.has_snapshot && (
          <span
            title="已留存结果快照，点击秒开预览"
            className="shrink-0 rounded-md bg-[var(--info-soft)]/15 px-1.5 py-0.5 text-micro text-[var(--info-text-2)]"
          >
            快照
          </span>
        )}
        <span className="shrink-0 text-caption text-[var(--text-faint)] transition-opacity group-hover/single:opacity-0">
          {formatRelativeTime(item.last_searched_at)}
        </span>
      </button>
      <DeleteHistoryButton
        label={`删除搜索历史：${item.keyword}`}
        onClick={onRemove}
        // !absolute 是必需的：.touch-target 在移动端的无层 position:relative
        // 会顶掉普通 absolute（同 media-track-rows / sidebar 的既有处理）
        className="touch-reveal !absolute right-2 top-1/2 -translate-y-1/2 opacity-0 group-hover/single:opacity-100"
      />
    </li>
  );
}

/** 展开态中的具体搜索范围：不重复关键词，只显示垂直、分类、快照和时间。 */
function HistoryVariantRow({
  item,
  onPick,
  onRemove,
}: {
  item: SearchHistoryItem;
  onPick: () => void;
  onRemove: () => void;
}) {
  return (
    <li className="group/variant flex items-center rounded-lg transition-colors hover:bg-white/[0.05]">
      <button
        type="button"
        onClick={onPick}
        className="flex min-w-0 flex-1 items-center gap-2 py-1.5 pl-2 text-left"
      >
        <span className="min-w-0 flex-1 truncate text-sub text-[var(--text-muted)]">
          {item.vertical === "titles" ? "影视" : `资源 · ${item.label ?? "全部"}`}
        </span>
        {item.has_snapshot && (
          <span className="shrink-0 rounded-md bg-[var(--info-soft)]/15 px-1.5 py-0.5 text-micro text-[var(--info-text-2)]">
            快照
          </span>
        )}
        <span className="shrink-0 text-micro text-[var(--text-faint)]">
          {formatRelativeTime(item.last_searched_at)}
        </span>
      </button>
      <DeleteHistoryButton
        label={`删除搜索历史：${item.keyword}（${item.vertical === "titles" ? "影视" : item.label ?? "资源全部"}）`}
        onClick={onRemove}
        className="touch-reveal mr-1 opacity-0 group-hover/variant:opacity-100"
      />
    </li>
  );
}

/** 单记录组在主行直接显示垂直与资源分类，不必展开才能辨认。 */
function HistoryTypeBadges({ item }: { item: SearchHistoryItem }) {
  const isMedia = item.vertical === "titles";
  return (
    <>
      <span
        className={`shrink-0 rounded-md px-1.5 py-0.5 text-micro ${
          isMedia
            ? "bg-[var(--accent-soft)] text-[var(--accent-2)]"
            : "bg-white/[0.07] text-[var(--text-muted)]"
        }`}
      >
        {isMedia ? "影视" : "资源"}
      </span>
      {!isMedia && item.label && (
        <span className="shrink-0 rounded-md bg-white/[0.07] px-1.5 py-0.5 text-micro text-[var(--text-muted)]">
          {item.label}
        </span>
      )}
    </>
  );
}

/** 组与子记录共用的删除按钮；mousedown 不抢走搜索输入框焦点。 */
function DeleteHistoryButton({
  label,
  onClick,
  className,
}: {
  label: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      // touch-target：21px 的裸 X 键是移动端删除历史的唯一入口，命中区
      // 撑到 44px（伪元素方案，外观不变）——紧贴相邻可点行，太小必误触
      className={`touch-target rounded-md p-1 text-[var(--text-faint)] transition-opacity hover:bg-white/[0.1] hover:text-[var(--text-muted)] ${className ?? ""}`}
    >
      <svg
        viewBox="0 0 24 24"
        className="size-[13px]"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        aria-hidden="true"
      >
        <path d="m6 6 12 12M18 6 6 18" />
      </svg>
    </button>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded bg-white/[0.07] px-1 py-px font-sans text-micro text-[var(--text-muted)]">
      {children}
    </kbd>
  );
}
