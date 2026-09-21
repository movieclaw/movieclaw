"use client";

import { useEffect, useRef, useState } from "react";

import { LiquidGlassButton } from "@/components/liquid-glass";

import { useConfirm } from "@/components/feedback";
import { GripIcon } from "@/components/icons";
import { listConfiguredSites, listSiteCatalog } from "@/lib/api/sites";
import { useBackdrop } from "@/lib/backdrop";
import {
  CATEGORY_LABEL,
  CATEGORY_OPTIONS,
  type PresetTab,
  type SearchTab,
  type TorrentCategory,
} from "@/lib/categories";
import { HttpError } from "@/lib/http";
import { useSearchPrefs } from "@/lib/search-prefs";
import { nanoid } from "nanoid";

/**
 * 拖拽排序的进行时状态。列表行数有限（8 个内置分类 + 至多 20 个预设），
 * 指针移动时整组重渲染的开销可忽略，直接放 useState 里换取
 * 「其余行实时让位」的动画简洁性。
 *
 * 坐标算法（行高不等也成立——预设行/带说明的分类行有第二行文字）：
 * - 按下时记录每一行的**原始中心 Y**（centers）与被拖行的高度；
 * - 拖动中，被拖行中心 = 原中心 + dy；目标下标 to = 中心在其上方的「其他行」数
 *   ——即把被拖行抽出后应插入的位置；
 * - 其余行按 from/to 的区间关系整体平移 ±被拖行高度（CSS transform 过渡），
 *   实时腾出落点。
 */
interface DragState {
  /** 被拖行的原始下标 */
  from: number;
  /** 当前应落入的目标下标（等于 from 表示位置未变） */
  to: number;
  /** 指针相对按下点的纵向位移 */
  dy: number;
  /** 按下时的指针 Y（视口坐标） */
  startY: number;
  /** 被拖行高度：其余行让位的平移距离 */
  height: number;
  /** 按下时每一行的中心 Y（视口坐标） */
  centers: number[];
}

/**
 * 内置分类的补充说明（展示在分类行名称下方）：只给语义不自明的分类写，
 * 电影/剧集这类一望即知的不加，避免整列说明文字变成噪音。
 */
const CATEGORY_HINT: Partial<Record<TorrentCategory, string>> = {
  av: "默认隐藏；打开后搜索面板会出现「成人」分类",
  other:
    "兜底分类：站点上归不进以上类别的分区（软件、电子书、体育、综艺等，各站范围不同）",
};

/** 预设编辑器的会话状态：新建（editingId=null）或编辑某个预设。 */
interface EditorState {
  /** 正在编辑的预设 id；null = 新建 */
  editingId: string | null;
  name: string;
  categories: TorrentCategory[];
  siteIds: string[];
  /** 图览模式：用该分类搜索时，结果页默认以图墙展示 */
  posterMode: boolean;
  /** 无痕搜索：用该分类搜索时不写入搜索历史 */
  skipHistory: boolean;
}

/** 站点勾选器的选项：已配置站点 + 展示名 + 当前是否可用。 */
interface SiteOption {
  siteId: string;
  displayName: string;
  usable: boolean;
}

/**
 * 搜索分类配置：搜索面板分类栏的完整设置，作为「资源站点」分区的
 * 「搜索分类」标签页（分类筛的就是同分区接入的那些站点，不单设分区）。
 *
 * 一个统一混排的标签列表：内置分类（不可删只可隐藏）与自定义分类（可增删改）
 * 同列拖拽排序、同款显隐开关；「全部」固定在搜索面板首位，不在此列表中。
 *
 * 自定义分类 = 命名的「分类组合 × 站点组合」预设：新建入口在父分区的工具栏
 * （与「添加站点」同位同款主按钮），经 ``createRequest`` 信号触发本组件打开
 * 编辑器（名称 + 分类勾选 + 站点勾选），分类/站点都不勾选表示「不限」。
 * 站点勾选器只列**已配置**站点；暂时不可用（禁用/验证未通过）的照样可勾选，
 * 搜索时会自动跳过。
 *
 * 所有改动即时保存到服务端（乐观更新，失败回滚并显示错误），无需保存按钮；
 * 编辑器内的「保存」也是同一条链路，失败时编辑器保持打开。
 *
 * 拖拽用原生 Pointer Events 手写（约 60 行），不为此引入 dnd 库：
 * 场景是单列定长小列表，库的能力（多容器、虚拟滚动、传感器抽象）全用不上。
 */
export function SearchSection({ createRequest = 0 }: { createRequest?: number }) {
  const { backdrop } = useBackdrop();
  const confirm = useConfirm();
  const { tabs, loading, saveTabs } = useSearchPrefs();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  // 父分区工具栏的「新建自定义分类」按钮：每点一次计数 +1，这里响应信号
  // 打开空白编辑器（初始 0 不触发，避免挂载即弹编辑器）
  useEffect(() => {
    if (createRequest > 0) openEditor(null);
  }, [createRequest]);

  /** 统一的保存包装：清错误 + busy 防抖 + 中文错误回显；返回是否成功。 */
  const apply = async (next: SearchTab[]): Promise<boolean> => {
    setError(null);
    setBusy(true);
    try {
      await saveTabs(next);
      return true;
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "保存失败，请检查网络后重试");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const toggle = (index: number, visible: boolean) =>
    void apply(tabs.map((t, i) => (i === index ? { ...t, visible } : t)));

  /** 把 from 行抽出并插入 to 位置，返回新数组。 */
  const reorder = (from: number, to: number) => {
    const next = [...tabs];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    return next;
  };

  /** 键盘微调：手柄聚焦时 ↑/↓ 与相邻行交换（拖拽的无障碍兜底）。 */
  const nudge = (index: number, delta: -1 | 1) => {
    const target = index + delta;
    if (target < 0 || target >= tabs.length) return;
    void apply(reorder(index, target));
  };

  // ---- 拖拽三事件：都挂在手柄上，setPointerCapture 后移动/抬起始终回到手柄 ----

  const onDragStart = (index: number) => (e: React.PointerEvent<HTMLButtonElement>) => {
    if (busy || loading) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // 无活动指针（合成事件等）时 capture 会抛错；丢失捕获只影响
      // 指针移出手柄后的跟踪，不应让拖拽直接失效
    }
    const rects = rowRefs.current.slice(0, tabs.length).map((el) => el?.getBoundingClientRect());
    if (rects.some((r) => !r)) return;
    setDrag({
      from: index,
      to: index,
      dy: 0,
      startY: e.clientY,
      height: rects[index]!.height,
      centers: rects.map((r) => r!.top + r!.height / 2),
    });
  };

  const onDragMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    setDrag((d) => {
      if (!d) return d;
      const dy = e.clientY - d.startY;
      const center = d.centers[d.from] + dy;
      // 目标下标 = 中心仍在被拖行中心之上的「其他行」数
      let to = 0;
      for (let i = 0; i < d.centers.length; i++) {
        if (i !== d.from && d.centers[i] < center) to++;
      }
      return { ...d, dy, to };
    });
  };

  const onDragEnd = () => {
    if (!drag) return;
    const { from, to } = drag;
    setDrag(null);
    if (from !== to) void apply(reorder(from, to));
  };

  /** 拖动中每一行的内联样式：被拖行跟随指针并抬起，其余行让位平移。 */
  const rowStyle = (index: number): React.CSSProperties => {
    if (!drag) return {};
    if (index === drag.from) {
      return { transform: `translateY(${drag.dy}px) scale(1.02)`, zIndex: 10 };
    }
    const { from, to, height } = drag;
    let shift = 0;
    if (from < to && index > from && index <= to) shift = -height;
    if (to < from && index >= to && index < from) shift = height;
    return { transform: `translateY(${shift}px)`, transition: "transform 200ms ease" };
  };

  /** 打开编辑器：新建（空草稿）或载入某个预设的当前值。 */
  const openEditor = (preset: PresetTab | null) => {
    setError(null);
    setEditor(
      preset
        ? {
            editingId: preset.id,
            name: preset.name,
            categories: preset.categories,
            siteIds: preset.site_ids,
            posterMode: preset.poster_mode,
            skipHistory: preset.skip_history,
          }
        : {
            editingId: null,
            name: "",
            categories: [],
            siteIds: [],
            posterMode: false,
            skipHistory: false,
          },
    );
  };

  /** 编辑器保存：新建追加到列表末尾（默认可见），编辑则原位替换（保留显隐）。 */
  const savePreset = async (draft: EditorState) => {
    const next: SearchTab[] = draft.editingId
      ? tabs.map((t) =>
          t.type === "preset" && t.id === draft.editingId
            ? {
                ...t,
                name: draft.name,
                categories: draft.categories,
                site_ids: draft.siteIds,
                poster_mode: draft.posterMode,
                skip_history: draft.skipHistory,
              }
            : t,
        )
      : [
          ...tabs,
          {
            type: "preset",
            // 前端生成短随机 id：偏好是整体覆盖式保存，后端无法区分新旧行
            id: `p-${nanoid(8)}`,
            name: draft.name,
            visible: true,
            categories: draft.categories,
            site_ids: draft.siteIds,
            poster_mode: draft.posterMode,
            skip_history: draft.skipHistory,
          },
        ];
    if (await apply(next)) setEditor(null);
  };

  const deletePreset = async (preset: PresetTab) => {
    const ok = await confirm({
      title: `删除自定义分类「${preset.name}」？`,
      description: "搜索历史不受影响。",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    if (editor?.editingId === preset.id) setEditor(null);
    void apply(tabs.filter((t) => !(t.type === "preset" && t.id === preset.id)));
  };

  return (
    <div className="space-y-5">
      <section>
        {/* divide-y 常驻：拖动中若移除分隔线会让整列高度跳 1px×行数，按下瞬间坐标漂移 */}
        <div className="css-glass select-none divide-y divide-white/[0.055] !rounded-2xl">
          {tabs.map((tab, index) => (
            <div
              key={`${tab.type}:${tab.id}`}
              ref={(el) => {
                rowRefs.current[index] = el;
              }}
              style={rowStyle(index)}
              className={`relative flex items-center gap-3 px-5 py-3.5 ${
                loading ? "opacity-50" : ""
              } ${
                drag?.from === index
                  ? "rounded-xl bg-[#1d222d]/95 shadow-[0_16px_40px_-12px_rgba(0,0,0,0.75)] ring-1 ring-white/[0.16] backdrop-blur-md"
                  : ""
              }`}
            >
              {/* 拖拽手柄：按住拖动排序；聚焦后 ↑/↓ 微调。touch-none 禁掉触屏滚动手势 */}
              <button
                type="button"
                aria-label={`拖动调整「${tabLabel(tab)}」的顺序，或按方向键上下移动`}
                disabled={busy || loading}
                onPointerDown={onDragStart(index)}
                onPointerMove={onDragMove}
                onPointerUp={onDragEnd}
                onPointerCancel={onDragEnd}
                onKeyDown={(e) => {
                  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                    e.preventDefault();
                    nudge(index, e.key === "ArrowUp" ? -1 : 1);
                  }
                }}
                className={`-ml-1.5 touch-none rounded-md p-1 text-[var(--text-faint)] transition-colors hover:bg-white/[0.08] hover:text-[var(--text)] focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:pointer-events-none disabled:opacity-30 ${
                  drag?.from === index ? "cursor-grabbing text-[var(--text)]" : "cursor-grab"
                }`}
              >
                <GripIcon className="size-4" />
              </button>

              {tab.type === "category" ? (
                <div className="min-w-0 flex-1">
                  <p className="text-body font-medium text-[var(--text)]">
                    {CATEGORY_LABEL[tab.id]}
                  </p>
                  {CATEGORY_HINT[tab.id] && (
                    <p className="mt-0.5 text-caption text-[var(--text-faint)]">
                      {CATEGORY_HINT[tab.id]}
                    </p>
                  )}
                </div>
              ) : (
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="truncate text-body font-medium text-[var(--text)]">{tab.name}</p>
                    <span className="shrink-0 rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-micro font-semibold text-[var(--accent)]">
                      自定义
                    </span>
                  </div>
                  <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
                    {presetSummary(tab)}
                  </p>
                </div>
              )}

              {tab.type === "preset" && (
                <div className="flex shrink-0 items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => openEditor(tab)}
                    disabled={busy || loading}
                    className="btn-glass px-2.5 py-1 text-caption font-medium disabled:opacity-40"
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    onClick={() => void deletePreset(tab)}
                    disabled={busy || loading}
                    className="rounded-full px-2.5 py-1 text-caption font-medium text-[var(--text-faint)] transition-colors hover:bg-[var(--danger)]/15 hover:text-[var(--danger)] disabled:opacity-40"
                  >
                    删除
                  </button>
                </div>
              )}

              {/* 显隐开关（真实 WebGL 液态玻璃开关，受控模式） */}
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                checked={tab.visible}
                disabled={busy || loading}
                onCheckedChange={(checked) => toggle(index, checked)}
                aria-label={`在搜索分类中${tab.visible ? "隐藏" : "展示"}「${tabLabel(tab)}」`}
                className="!min-h-0 !w-auto !bg-transparent !p-0"
              >
                <span className="sr-only">{tabLabel(tab)}</span>
              </LiquidGlassButton>
            </div>
          ))}
        </div>

      </section>

      {editor && (
        <PresetEditor
          draft={editor}
          busy={busy}
          onChange={setEditor}
          onSave={() => void savePreset(editor)}
          onCancel={() => {
            setEditor(null);
            setError(null);
          }}
        />
      )}

      {error && (
        <p className="rounded-xl border border-[var(--danger)]/30 bg-[var(--danger)]/10 px-4 py-2.5 text-sub text-[var(--danger)]">
          {error}
        </p>
      )}

      <p className="text-sub leading-6 text-[var(--text-faint)]">
        按住左侧手柄拖动即可调整顺序——列表顺序即搜索面板中分类标签的排列顺序，「全部」固定在首位。
        自定义分类可组合多个资源分类与指定站点，一次搜索只打勾选的站点。
        改动即时保存到服务端，所有设备与浏览器保持一致。
      </p>
    </div>
  );
}

/** 标签的展示名（内置分类取中文名，预设取用户起的名字）。 */
function tabLabel(tab: SearchTab): string {
  return tab.type === "category" ? CATEGORY_LABEL[tab.id] : tab.name;
}

/** 预设行的摘要：分类组合 × 站点组合（× 图览 × 无痕），空集显示「不限 / 全部」。 */
function presetSummary(tab: PresetTab): string {
  const cats = tab.categories.length
    ? tab.categories.map((c) => CATEGORY_LABEL[c]).join("、")
    : "不限分类";
  const sites = tab.site_ids.length ? `${tab.site_ids.length} 个站点` : "全部站点";
  const flags = `${tab.poster_mode ? " · 图览" : ""}${tab.skip_history ? " · 无痕" : ""}`;
  return `${cats} · ${sites}${flags}`;
}

/**
 * 预设编辑器：名称 + 分类勾选 + 站点勾选。
 *
 * 站点选项在打开时向后端拉取（已配置站点 ∩ 站点目录，取展示名与可用状态）；
 * 分类/站点都不勾选表示「不限」，用 chips 的空选态自然表达，不设「全选」按钮。
 */
function PresetEditor({
  draft,
  busy,
  onChange,
  onSave,
  onCancel,
}: {
  draft: EditorState;
  busy: boolean;
  onChange: (next: EditorState) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { backdrop } = useBackdrop();
  // null = 加载中；[] = 没有任何已配置站点
  const [siteOptions, setSiteOptions] = useState<SiteOption[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listConfiguredSites(), listSiteCatalog()])
      .then(([configured, catalog]) => {
        if (cancelled) return;
        const nameOf = new Map(catalog.map((c) => [c.site_id, c.display_name]));
        setSiteOptions(
          configured.map((s) => ({
            siteId: s.site_id,
            displayName: nameOf.get(s.site_id) ?? s.site_id,
            usable: s.usable,
          })),
        );
      })
      .catch(() => !cancelled && setSiteOptions([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const toggleCategory = (id: TorrentCategory) =>
    onChange({
      ...draft,
      categories: draft.categories.includes(id)
        ? draft.categories.filter((c) => c !== id)
        : [...draft.categories, id],
    });

  const toggleSite = (id: string) =>
    onChange({
      ...draft,
      siteIds: draft.siteIds.includes(id)
        ? draft.siteIds.filter((s) => s !== id)
        : [...draft.siteIds, id],
    });

  const chipCls = (active: boolean) =>
    `rounded-full border px-3 py-1 text-sub transition-colors ${
      active
        ? "border-[var(--accent)]/60 bg-[var(--accent)]/15 text-[var(--accent)]"
        : "border-white/[0.09] bg-white/[0.04] text-[var(--text-muted)] hover:border-white/[0.18] hover:text-[var(--text)]"
    }`;

  return (
    <section>
      <h3 className="group-label mb-2.5 px-1">
        {draft.editingId ? "编辑自定义分类" : "新建自定义分类"}
      </h3>
      <div className="css-glass space-y-5 !rounded-2xl p-5">
        <div>
          <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">
            名称（1~16 字）
          </label>
          <input
            type="text"
            value={draft.name}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            maxLength={16}
            placeholder="如：4K 影剧、MT 专搜"
            autoFocus
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
          />
        </div>

        <div>
          <p className="mb-1.5 text-sub font-medium text-[var(--text-muted)]">
            资源分类 <span className="text-[var(--text-faint)]">（不勾选 = 不限分类）</span>
          </p>
          <div className="flex flex-wrap gap-1.5">
            {CATEGORY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => toggleCategory(opt.value)}
                className={chipCls(draft.categories.includes(opt.value))}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        <div>
          <p className="mb-1.5 text-sub font-medium text-[var(--text-muted)]">
            搜索站点 <span className="text-[var(--text-faint)]">（不勾选 = 全部可用站点）</span>
          </p>
          {siteOptions === null ? (
            <p className="text-sub text-[var(--text-faint)]">正在加载站点列表…</p>
          ) : siteOptions.length === 0 ? (
            <p className="text-sub text-[var(--text-faint)]">
              还没有接入任何站点；先切到「站点接入」标签页添加，或直接保存（默认搜全部可用站点）。
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {siteOptions.map((site) => (
                <button
                  key={site.siteId}
                  type="button"
                  onClick={() => toggleSite(site.siteId)}
                  title={site.usable ? undefined : "站点当前不可用（未启用或验证未通过），搜索时会自动跳过"}
                  className={`${chipCls(draft.siteIds.includes(site.siteId))} ${
                    site.usable ? "" : "opacity-55"
                  }`}
                >
                  {site.displayName}
                  {!site.usable && <span className="ml-1 text-micro">·不可用</span>}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-sub font-medium text-[var(--text-muted)]">图览模式</p>
            <p className="mt-0.5 text-caption leading-4 text-[var(--text-faint)]">
              用该分类搜索时，带海报的结果默认以图墙展示（仅部分站点返回海报，
              如 M-Team）；结果页右上角可随时临时切换。
            </p>
          </div>
          <LiquidGlassButton
            backgroundImage={backdrop}
            variant="dark"
            checked={draft.posterMode}
            onCheckedChange={(checked) => onChange({ ...draft, posterMode: checked })}
            aria-label={`${draft.posterMode ? "关闭" : "开启"}图览模式`}
            className="!min-h-0 !w-auto !bg-transparent !p-0"
          >
            <span className="sr-only">图览模式</span>
          </LiquidGlassButton>
        </div>

        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-sub font-medium text-[var(--text-muted)]">无痕搜索</p>
            <p className="mt-0.5 text-caption leading-4 text-[var(--text-faint)]">
              用该分类搜索时不写入搜索历史，搜索面板的「最近搜索」不会出现相关记录，
              适合隐私敏感的分类。
            </p>
          </div>
          <LiquidGlassButton
            backgroundImage={backdrop}
            variant="dark"
            checked={draft.skipHistory}
            onCheckedChange={(checked) => onChange({ ...draft, skipHistory: checked })}
            aria-label={`${draft.skipHistory ? "关闭" : "开启"}无痕搜索`}
            className="!min-h-0 !w-auto !bg-transparent !p-0"
          >
            <span className="sr-only">无痕搜索</span>
          </LiquidGlassButton>
        </div>

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={busy || !draft.name.trim()}
            className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold disabled:opacity-40"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </section>
  );
}
