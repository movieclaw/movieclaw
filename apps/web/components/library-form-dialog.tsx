"use client";

import { useEffect, useRef, useState } from "react";

import { DirectoryPicker } from "@/components/directory-picker";
import {
  CheckIcon,
  ChevronDownIcon,
  FolderIcon,
  InfoIcon,
  PlusIcon,
  XIcon,
} from "@/components/icons";
import { LibraryScrapeSettings } from "@/components/library-scrape-settings";
import { LIBRARY_KIND_META } from "@/components/library-view";
import { Modal } from "@/components/modal";
import { Tooltip } from "@/components/tooltip";
import {
  type LibraryPayload,
  type MatchRule,
  type MediaLibrary,
  type RoutingOptions,
  createLibrary,
  listLibraryRoutingOptions,
  updateLibrary,
  type LibraryAccessMode,
} from "@/lib/api/libraries";
import { listMembers, type MemberView } from "@/lib/api/members";
import type { LibraryKind } from "@/lib/media-types";

/**
 * 媒体库表单弹窗（新建向导 / 编辑分区），从 library-view.tsx 拆出：
 * 首页只做浏览入口，建库与编辑落在管理页（docs/design/library-manage.md），
 * 单库页头部的「编辑库」也用这里的 LibraryFormDialog。
 */
/** 收藏范围可选项的模块级缓存：这是后端静态常量，但 useRoutingOptions 被
 *  每张库卡片和表单弹窗各自调用——不缓存的话库首页一次挂载就打出 N 个
 *  相同请求。失败时清空缓存，下一个调用方重试。 */
let routingOptionsPromise: Promise<RoutingOptions> | null = null;

/** 收藏范围可选项（后端静态常量）；加载失败降级为 null（相关 UI 不渲染）。 */
function useRoutingOptions(): RoutingOptions | null {
  const [options, setOptions] = useState<RoutingOptions | null>(null);
  useEffect(() => {
    let cancelled = false;
    routingOptionsPromise ??= listLibraryRoutingOptions();
    void routingOptionsPromise
      .then((o) => {
        if (!cancelled) setOptions(o);
      })
      .catch(() => {
        /* 静默降级：收藏范围区显示加载失败提示 */
        routingOptionsPromise = null;
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return options;
}

/** 收藏范围条件 → 表单状态（v1 两个维度：类型 ID / 区域国家码）。 */
function parseMatchRules(rules: MatchRule[]): { genres: number[]; regions: string[] } {
  const genres = rules.find((r) => r.field === "genres")?.values ?? [];
  const regions = rules.find((r) => r.field === "origin_countries")?.values ?? [];
  return {
    genres: genres.filter((v): v is number => typeof v === "number"),
    regions: regions.filter((v): v is string => typeof v === "string"),
  };
}

/** 表单状态 → 收藏范围条件（空维度不生成条件；两者都空 = 不声明）。 */
function buildMatchRules(genres: number[], regions: string[]): MatchRule[] {
  const rules: MatchRule[] = [];
  if (genres.length > 0) rules.push({ field: "genres", op: "any_of", values: genres });
  if (regions.length > 0)
    rules.push({ field: "origin_countries", op: "any_of", values: regions });
  return rules;
}

/** 把区域国家码折叠成展示名：整组命中的折叠成预设组名（如「日韩」），
 *  折不进组的逐个显示中文名。用于表单弹窗底栏的当前声明摘要。 */
function regionLabels(regions: string[], options: RoutingOptions): string[] {
  const parts: string[] = [];
  let rest = [...regions];
  for (const preset of options.region_presets) {
    if (preset.countries.every((c) => rest.includes(c))) {
      parts.push(preset.label);
      rest = rest.filter((c) => !preset.countries.includes(c));
    }
  }
  parts.push(...rest.map((c) => options.country_names[c] ?? c));
  return parts;
}

/* —— 媒体库表单：新建极简（向导），编辑分层（分区折叠） ——
 *
 * 新建只问必答题——类型、名称与目录、（影视库）收藏范围；四个扫描开关全有
 * 安全默认值，刮削偏好覆盖是少数人的精调，都放到编辑里。编辑按用途分区折叠，
 * 折叠态的摘要行先回答"这个库配成了什么样"，改哪项点哪项。
 * 设计稿：docs/design/library-other-kind.md 4.3 的表单节。 */

const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui " +
  "text-[var(--text)] outline-none focus:border-[var(--accent)]/60";
const LABEL_CLASS = "mb-1.5 block text-sub font-medium text-[var(--text-muted)]";

/** 类型卡片的文案：讲清后果，不提识别用的是哪个数据源；「其他」放什么由用户定。 */
const KIND_CARDS: Record<
  LibraryKind,
  { blurb: string; traits: string; chosen: string; defaultName: string; defaults: string }
> = {
  movie: {
    blurb: "自动识别影片，补齐简介、评分、演职员与海报剧照；一部片一个目录。",
    traits: "可订阅 · 可整理文件名",
    chosen: "自动识别并补齐元数据与图片",
    defaultName: "电影库",
    defaults:
      "实时监控目录变化、扫描后保留丢失记录、为未识别文件生成缩略图、在首页展示。" +
      "第一个电影库自动成为默认库；刮削偏好建好后在「编辑库」里设。",
  },
  tv: {
    blurb: "自动识别剧集与季集，补齐分集信息与图片；追新订阅按集补齐。",
    traits: "可订阅 · 可整理文件名",
    chosen: "自动识别季集并补齐元数据与图片",
    defaultName: "剧集库",
    defaults:
      "实时监控目录变化、扫描后保留丢失记录、为未识别文件生成缩略图、在首页展示。" +
      "第一个剧集库自动成为默认库；刮削偏好建好后在「编辑库」里设。",
  },
  video: {
    blurb:
      "不识别、不刮削、不改名的视频：放什么由你定。有 NFO 就读 NFO，否则按文件名展示，封面从视频里抓帧。",
    traits: "不识别 · 可播放 · 记进度",
    chosen: "不识别不刮削，按 NFO / 文件名展示",
    defaultName: "其他",
    defaults:
      "实时监控目录变化、扫描后保留丢失记录、从视频抓帧生成缩略图、在首页展示。" +
      "网络挂载的目录建议建好后到「编辑库 → 扫描与监控」关掉实时监控与抓帧。",
  },
};

/** 新建时的类型是否带识别链（决定有没有收藏范围这一步）；已有库读服务端能力位。 */
function kindIsScraped(kind: LibraryKind): boolean {
  return kind !== "video";
}

/** 根目录列表：第一项为主根；行内可原位更改、设为主根、移除。 */
function RootsEditor({
  roots,
  onChange,
  onPick,
}: {
  roots: string[];
  onChange: (next: string[]) => void;
  /** 打开目录选择器："add"=追加，数字=原位更改该下标 */
  onPick: (target: "add" | number) => void;
}) {
  return (
    <div className="space-y-1.5">
      {roots.map((root, i) => (
        <div
          key={root}
          className="group flex items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2"
        >
          <FolderIcon className="size-4 shrink-0 text-[var(--accent)]/80" />
          {/* 保尾截断（dir=rtl）：路径的区分信息在尾部，省略号出现在头部；
              LRM 标记防止首尾的 "/" 在 RTL 下跳位。点击可原位更改目录 */}
          <Tooltip
            content={
              <>
                <p className="mb-1 break-all font-mono text-caption text-[var(--text-muted)]">{root}</p>
                点击更改：从当前路径开始重新选择目录。
              </>
            }
          >
            <button
              type="button"
              dir="rtl"
              onClick={() => onPick(i)}
              className="min-w-0 flex-1 truncate rounded text-left font-mono text-ui text-[var(--text)] transition-colors hover:text-[var(--accent)]"
            >
              {"‎" + root + "‎"}
            </button>
          </Tooltip>
          {i === 0 ? (
            <Tooltip
              content={
                <>
                  <strong>主根 = 新内容的落盘位置。</strong>
                  订阅与手动下载完成后，按「主根/标题 (年份)」建目录入库；
                  一个库可挂多个根，但写入点只有主根这一个。
                </>
              }
            >
              <span className="shrink-0 cursor-default rounded-full bg-[var(--accent)]/15 px-2 py-0.5 text-micro font-semibold text-[var(--accent)]">
                主根
              </span>
            </Tooltip>
          ) : (
            <Tooltip content="把该路径设为新内容的落盘位置（移到列表第一位）。已有文件不会被移动。">
              <button
                type="button"
                onClick={() => onChange([root, ...roots.filter((r) => r !== root)])}
                className="touch-reveal shrink-0 rounded-full px-2 py-0.5 text-micro font-medium text-[var(--text-faint)] opacity-0 transition-opacity hover:bg-white/10 hover:text-white group-hover:opacity-100"
              >
                设为主根
              </button>
            </Tooltip>
          )}
          <button
            type="button"
            aria-label={`移除 ${root}`}
            onClick={() => onChange(roots.filter((r) => r !== root))}
            className="shrink-0 rounded-md p-1 text-[var(--text-faint)] transition-colors hover:bg-white/10 hover:text-white"
          >
            <XIcon className="size-3.5" />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => onPick("add")}
        className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-white/15 px-3 py-2.5 text-ui font-medium text-[var(--text-muted)] transition-colors hover:border-[var(--accent)]/50 hover:text-white"
      >
        <PlusIcon className="size-4" />
        {roots.length === 0 ? "浏览服务器目录并添加" : "添加目录"}
      </button>
    </div>
  );
}

/** 服务端目录选择器的接线：追加时从最近添加的根起步，更改时从被改的根起步；
 *  追加去重，更改为原位替换（改主根仍是主根），撞上已有路径时合并去重。 */
function RootsPicker({
  target,
  roots,
  onChange,
  onClose,
}: {
  target: "add" | number | null;
  roots: string[];
  onChange: (next: string[]) => void;
  onClose: () => void;
}) {
  return (
    <DirectoryPicker
      open={target !== null}
      initialPath={
        target === "add" || target === null
          ? roots.length > 0
            ? roots[roots.length - 1]
            : undefined
          : roots[target]
      }
      onClose={onClose}
      onSelect={(path) => {
        if (target === "add" || target === null) {
          onChange(roots.includes(path) ? roots : [...roots, path]);
        } else {
          const next = roots.map((r, idx) => (idx === target ? path : r));
          onChange(next.filter((r, idx) => r !== path || idx === target));
        }
        onClose();
      }}
    />
  );
}

/** 收藏范围：区域逐国勾选（预设组是一键整组的快捷键）+ 类型多选，两个维度间是"且"。 */
function ScopeEditor({
  kind,
  regions,
  genres,
  onRegions,
  onGenres,
  options,
}: {
  kind: LibraryKind;
  regions: string[];
  genres: number[];
  onRegions: (next: string[]) => void;
  onGenres: (next: number[]) => void;
  options: RoutingOptions | null;
}) {
  if (options === null) {
    return (
      <p className="rounded-xl border border-white/[0.08] bg-white/[0.03] px-3 py-2.5 text-sub text-[var(--text-faint)]">
        正在加载可选项…
      </p>
    );
  }
  const genreOptions = kind === "movie" ? options.movie_genres : options.tv_genres;
  const allCountries = Object.keys(options.country_names);
  return (
    <>
      <div>
        <label className={LABEL_CLASS}>区域（勾选任一即匹配）</label>
        {/* 已选中但不在内置映射里的码补进列表，保证选了就能看见、能取消 */}
        <div className="flex flex-wrap gap-1.5">
          {[
            ...Object.entries(options.country_names),
            ...regions
              .filter((c) => !(c in options.country_names))
              .map((c): [string, string] => [c, c]),
          ].map(([code, label]) => (
            <button
              key={code}
              type="button"
              data-active={regions.includes(code)}
              onClick={() =>
                onRegions(
                  regions.includes(code) ? regions.filter((c) => c !== code) : [...regions, code],
                )
              }
              className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
            >
              {label}
            </button>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-caption text-[var(--text-faint)]">快捷组合</span>
          <button
            type="button"
            onClick={() =>
              onRegions(
                allCountries.every((c) => regions.includes(c))
                  ? []
                  : [...new Set([...regions, ...allCountries])],
              )
            }
            className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
          >
            {allCountries.every((c) => regions.includes(c)) ? "清空" : "全选"}
          </button>
          {options.region_presets.map((preset) => {
            const active = preset.countries.every((c) => regions.includes(c));
            return (
              <button
                key={preset.key}
                type="button"
                data-active={active}
                title={preset.countries.map((c) => options.country_names[c] ?? c).join(" / ")}
                onClick={() =>
                  onRegions(
                    active
                      ? regions.filter((c) => !preset.countries.includes(c))
                      : [...new Set([...regions, ...preset.countries])],
                  )
                }
                className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
              >
                {preset.label}
              </button>
            );
          })}
        </div>
      </div>
      <div>
        <label className={LABEL_CLASS}>类型（勾选任一即匹配）</label>
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={() =>
              onGenres(
                genreOptions.every((g) => genres.includes(g.id))
                  ? []
                  : [...new Set([...genres, ...genreOptions.map((g) => g.id)])],
              )
            }
            className="glass-row nav-item !w-auto px-2.5 py-1 text-caption font-medium"
          >
            {genreOptions.every((g) => genres.includes(g.id)) ? "清空" : "全选"}
          </button>
          {genreOptions.map((g) => (
            <button
              key={g.id}
              type="button"
              data-active={genres.includes(g.id)}
              onClick={() =>
                onGenres(
                  genres.includes(g.id) ? genres.filter((id) => id !== g.id) : [...genres, g.id],
                )
              }
              className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
            >
              {g.label}
            </button>
          ))}
        </div>
      </div>
      {regions.length > 0 && genres.length > 0 && (
        <p className="text-caption leading-relaxed text-[var(--text-faint)]">
          区域与类型须<strong className="font-medium text-[var(--text-muted)]">同时满足</strong>
          （如「日韩 + 动画」= 只收日韩的动画）。
        </p>
      )}
    </>
  );
}

/** 收藏范围的一句话摘要（新建第 3 步底部与编辑分区的折叠行共用）。 */
function scopeSummary(
  kind: LibraryKind,
  regions: string[],
  genres: number[],
  options: RoutingOptions | null,
): { declared: boolean; text: string } {
  if (options === null) {
    const declared = regions.length > 0 || genres.length > 0;
    return { declared, text: declared ? "已声明收藏范围" : "未声明" };
  }
  const genreOptions = kind === "movie" ? options.movie_genres : options.tv_genres;
  const parts = [
    regionLabels(regions, options).join(" / "),
    genreOptions
      .filter((g) => genres.includes(g.id))
      .map((g) => g.label)
      .join(" / "),
  ].filter(Boolean);
  return parts.length > 0
    ? { declared: true, text: parts.join(" 的 ") }
    : { declared: false, text: "未声明" };
}

/** 类型 ID 只保留当前库类型下有效的（切换过类型时另一类型独有的 ID 不带进声明）。 */
function validGenres(kind: LibraryKind, genres: number[], options: RoutingOptions | null): number[] {
  if (options === null) return genres;
  const valid = new Set((kind === "movie" ? options.movie_genres : options.tv_genres).map((g) => g.id));
  return genres.filter((id) => valid.has(id));
}

/** 表单入口：新建走向导，编辑走分区面板；调用方只关心 state 三态。 */
export function LibraryFormDialog({
  state,
  onClose,
  onSaved,
}: {
  /** "new"=新增；库对象=编辑；null=关闭 */
  state: MediaLibrary | "new" | null;
  onClose: () => void;
  /** 保存成功回调，带上服务端返回的库（新建时调用方据此把新卡片滚进视野） */
  onSaved: (saved: MediaLibrary) => void;
}) {
  if (state === null) return null;
  if (state === "new") return <CreateLibraryDialog onClose={onClose} onSaved={onSaved} />;
  return <EditLibraryDialog key={state.id} library={state} onClose={onClose} onSaved={onSaved} />;
}

/* —— 新建：选类型 → 名称与目录 → （影视库）收藏范围 —— */

function CreateLibraryDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: (saved: MediaLibrary) => void;
}) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [kind, setKind] = useState<LibraryKind | null>(null);
  const [name, setName] = useState("");
  const [roots, setRoots] = useState<string[]>([]);
  const [pickerTarget, setPickerTarget] = useState<"add" | number | null>(null);
  const [regions, setRegions] = useState<string[]>([]);
  const [genres, setGenres] = useState<number[]>([]);
  const [accessMode, setAccessMode] = useState<LibraryAccessMode>("everyone");
  const [adminVisible, setAdminVisible] = useState(true);
  const [memberIds, setMemberIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const routingOptions = useRoutingOptions();

  const hasScope = kind !== null && kindIsScraped(kind);
  const totalSteps = hasScope ? 3 : 2;
  const ready = name.trim().length > 0 && roots.length > 0;
  const scope = kind ? scopeSummary(kind, regions, genres, routingOptions) : null;

  const chooseKind = (next: LibraryKind) => {
    // 名称按类型预填；用户已经改过的名字不动
    const untouched = name.trim() === "" || (kind !== null && name === KIND_CARDS[kind].defaultName);
    setKind(next);
    if (untouched) setName(KIND_CARDS[next].defaultName);
    if (next === "video") {
      setRegions([]);
      setGenres([]);
    }
    setStep(2);
  };

  const submit = () => {
    if (kind === null || !ready || busy) return;
    setBusy(true);
    setError(null);
    const payload: LibraryPayload = {
      name: name.trim(),
      kind,
      root_paths: roots,
      match_rules: hasScope ? buildMatchRules(validGenres(kind, genres, routingOptions), regions) : [],
      // 四个开关全按推荐值：监控开、自动清理关、缩略图开、首页展示；建好后在编辑里调
      auto_clear_missing: false,
      realtime_watch: true,
      generate_thumbnails: true,
      exclude_from_home: false,
      scrape_overrides: {},
      access_mode: accessMode,
      admin_visible: adminVisible,
      member_ids: accessMode === "selected" ? memberIds : [],
    };
    void createLibrary(payload)
      .then(onSaved)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  // 主按钮随步骤变化：第 2 步对影视库是「下一步」，最后一步保存即扫描
  const primary =
    step === 1
      ? { label: "创建并开始扫描", disabled: true, action: () => {} }
      : step === 2 && hasScope
        ? { label: "下一步：收藏范围", disabled: !ready, action: () => setStep(3) }
        : {
            label: busy
              ? "创建中…"
              : step === 3 && scope !== null && !scope.declared
                ? "跳过，创建并开始扫描"
                : "创建并开始扫描",
            disabled: !ready || busy,
            action: submit,
          };
  const footNote =
    `第 ${step} 步，共 ${totalSteps} 步` + (step === totalSteps ? " · 保存即开始扫描存量文件" : "");

  return (
    <>
      <Modal open onClose={onClose} label="添加媒体库" width="2xl" panelClassName="flex max-h-[86dvh] flex-col">
        <div className="shrink-0 border-b border-white/[0.06] px-6 pt-5 max-md:px-5">
          <h2 className="text-title font-bold text-white">添加媒体库</h2>
          <ol className="mt-3.5 flex gap-5 text-caption font-semibold uppercase tracking-[0.06em]">
            {(
              [
                [1, "1 · 选类型"],
                [2, "2 · 名称与目录"],
                [3, "3 · 收藏范围"],
              ] as const
            )
              .filter(([n]) => n !== 3 || hasScope)
              .map(([n, label]) => (
                <li
                  key={n}
                  aria-current={step === n ? "step" : undefined}
                  className={`relative pb-2.5 ${step === n ? "text-white" : "text-[var(--text-faint)]"}`}
                >
                  {label}
                  {step === n && (
                    <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-[var(--accent)]" />
                  )}
                </li>
              ))}
          </ol>
        </div>

        <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto p-6 max-md:p-5">
          {step === 1 && (
            <>
              <div className="grid grid-cols-3 gap-2.5 max-md:grid-cols-1">
                {(Object.keys(LIBRARY_KIND_META) as LibraryKind[]).map((k) => {
                  const { label, Icon } = LIBRARY_KIND_META[k];
                  const card = KIND_CARDS[k];
                  return (
                    <button
                      key={k}
                      type="button"
                      onClick={() => chooseKind(k)}
                      data-active={kind === k}
                      className="flex min-h-[148px] flex-col gap-2.5 rounded-2xl border border-white/[0.08] bg-white/[0.04] p-4 text-left transition-colors hover:border-white/20 hover:bg-white/[0.075] data-[active=true]:border-[var(--accent-2)] data-[active=true]:bg-white/[0.12]"
                    >
                      <Icon className="size-[22px] text-[var(--accent)]" />
                      <span className="text-body font-semibold text-white">{label}</span>
                      <span className="text-caption leading-relaxed text-[var(--text-muted)]">{card.blurb}</span>
                      <span className="mt-auto text-micro text-[var(--text-faint)]">{card.traits}</span>
                    </button>
                  );
                })}
              </div>
              <p className="text-caption leading-relaxed text-[var(--text-faint)]">
                类型决定这个库怎么扫、怎么摆、能不能订阅，创建后不可更改。
              </p>
            </>
          )}

          {step === 2 && kind !== null && (
            <>
              {(() => {
                const { label, Icon } = LIBRARY_KIND_META[kind];
                return (
                  <div className="flex items-center gap-2.5 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2.5">
                    <Icon className="size-[18px] shrink-0 text-[var(--accent)]" />
                    <span className="font-semibold text-white">{label}</span>
                    <span className="min-w-0 truncate text-caption text-[var(--text-muted)]">
                      {KIND_CARDS[kind].chosen}
                    </span>
                    <button
                      type="button"
                      onClick={() => setStep(1)}
                      className="ml-auto shrink-0 text-caption text-[var(--text-faint)] underline underline-offset-4 hover:text-white"
                    >
                      换类型
                    </button>
                  </div>
                );
              })()}
              <div>
                <label className={LABEL_CLASS}>名称</label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    // isComposing：中文输入法选词的回车不当提交
                    if (e.key === "Enter" && !e.nativeEvent.isComposing && !primary.disabled) primary.action();
                  }}
                  placeholder="如：电影库 / 动漫库"
                  autoComplete="off"
                  className={INPUT_CLASS}
                />
              </div>
              <div>
                <label className={LABEL_CLASS}>
                  根目录
                  <span className="font-normal text-[var(--text-faint)]">（第一个为主根，新内容落在这里）</span>
                </label>
                <RootsEditor roots={roots} onChange={setRoots} onPick={setPickerTarget} />
              </div>
              <div>
                <label className={LABEL_CLASS}>可见范围</label>
                <AccessScopeEditor
                  mode={accessMode}
                  adminVisible={adminVisible}
                  memberIds={memberIds}
                  onMode={setAccessMode}
                  onAdminVisible={setAdminVisible}
                  onMemberIds={setMemberIds}
                />
              </div>
              <div className="flex items-start gap-2.5 rounded-xl border border-[var(--info)]/20 bg-[var(--info)]/[0.07] px-3.5 py-3 text-caption leading-relaxed text-[var(--text-muted)]">
                <InfoIcon className="mt-0.5 size-4 shrink-0 text-[var(--info)]" />
                <p>
                  <strong className="font-medium text-[var(--text)]">已按推荐值设好：</strong>
                  {KIND_CARDS[kind].defaults}
                </p>
              </div>
            </>
          )}

          {step === 3 && kind !== null && (
            <>
              <p className="text-sub leading-relaxed text-[var(--text-muted)]">
                声明「本库收什么」，订阅与自动入库就会按作品的区域和类型自动选进这个库。
                <strong className="font-medium text-[var(--text)]">全部留空也可以</strong>
                ：作为默认库承接所有未命中的作品；订阅时永远可以手动改库。
              </p>
              <ScopeEditor
                kind={kind}
                regions={regions}
                genres={genres}
                onRegions={setRegions}
                onGenres={setGenres}
                options={routingOptions}
              />
              <p className="text-caption leading-relaxed text-[var(--text-faint)]">
                {scope?.declared
                  ? `当前：收 ${scope.text}；其他作品去该类型的默认库。`
                  : "当前：未声明，本库将承接该类型全部未命中的作品。"}
              </p>
            </>
          )}
        </div>

        <div className="shrink-0 border-t border-white/[0.06] px-6 py-4 max-md:px-5">
          {error && (
            <p className="mb-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-ui leading-6 text-red-200">
              {error}
            </p>
          )}
          <div className="flex items-center justify-between gap-3">
            <p className="min-w-0 truncate text-caption text-[var(--text-faint)]">{footNote}</p>
            <div className="flex shrink-0 items-center gap-3">
              <button
                type="button"
                onClick={step === 1 ? onClose : () => setStep(step === 3 ? 2 : 1)}
                className="btn-glass h-9 px-4 text-ui font-medium"
              >
                {step === 1 ? "取消" : "上一步"}
              </button>
              <button
                type="button"
                onClick={primary.action}
                disabled={primary.disabled}
                className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
              >
                {primary.label}
              </button>
            </div>
          </div>
        </div>
      </Modal>
      <RootsPicker target={pickerTarget} roots={roots} onChange={setRoots} onClose={() => setPickerTarget(null)} />
    </>
  );
}

/* —— 编辑：分区折叠，每区一行摘要 —— */

type EditSectionId = "basic" | "scan" | "access" | "scope" | "scrape";

/**
 * 可见范围编辑器（docs/design/library-access.md 2.1）：
 * 「所有成员」= 对全部成员自动开放（含以后新建的）；「指定成员」= 只对勾选的
 * 成员开放，名单第一项固定是「超管（我自己）」——超管不是成员，进不了白名单，
 * 单独一位存在 admin_visible 上，但对用户就是同一份名单。
 * 管理权与浏览权分离：超管把自己摘掉后仍能管理这个库，只是看不到内容。
 */
function AccessScopeEditor({
  mode,
  adminVisible,
  memberIds,
  onMode,
  onAdminVisible,
  onMemberIds,
}: {
  mode: LibraryAccessMode;
  adminVisible: boolean;
  memberIds: number[];
  onMode: (next: LibraryAccessMode) => void;
  onAdminVisible: (next: boolean) => void;
  onMemberIds: (next: number[]) => void;
}) {
  const [members, setMembers] = useState<MemberView[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    listMembers()
      .then((rows) => {
        if (!cancelled) setMembers(rows.filter((m) => m.status === "active"));
      })
      .catch(() => {
        if (!cancelled) setMembers([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const modeButton = (value: LibraryAccessMode, label: string) => (
    <button
      type="button"
      onClick={() => onMode(value)}
      className={`rounded-md px-3 py-1.5 text-sub font-medium transition-colors ${
        mode === value ? "bg-white/[0.11] text-white" : "text-[var(--text-faint)] hover:text-[var(--text)]"
      }`}
    >
      {label}
    </button>
  );
  const nobody = mode === "selected" && !adminVisible && memberIds.length === 0;
  // 超管与成员同列一份名单：超管用固定 id "admin"，成员用数字 id
  const options: ViewerOption[] = [
    { id: ADMIN_OPTION_ID, label: "超管（我自己）", hint: "你自己" },
    ...(members ?? []).map((m) => ({
      id: m.id,
      label: m.nickname || m.username,
      hint: `@${m.username}`,
    })),
  ];
  const selected: ViewerOptionId[] = [
    ...(adminVisible ? [ADMIN_OPTION_ID as ViewerOptionId] : []),
    ...memberIds,
  ];
  const setSelected = (next: ViewerOptionId[]) => {
    onAdminVisible(next.includes(ADMIN_OPTION_ID));
    onMemberIds(next.filter((v): v is number => typeof v === "number"));
  };
  return (
    <div>
      <div className="inline-flex rounded-lg border border-white/[0.08] bg-white/[0.035] p-1">
        {modeButton("everyone", "所有成员")}
        {modeButton("selected", "指定成员")}
      </div>
      <p className="mt-2 text-caption leading-relaxed text-[var(--text-faint)]">
        {mode === "everyone"
          ? "对全部成员开放，包括以后新建的成员；成员管理页里切到「指定库」的成员除外。"
          : "只有选中的人能浏览这个库的内容；其他人在首页、搜索、最近观看和播放器里都看不到它。你始终可以管理它。"}
      </p>
      {mode === "selected" && (
        <div className="mt-3">
          <ViewerCombobox
            options={options}
            selected={selected}
            onChange={setSelected}
            loading={members === null}
          />
        </div>
      )}
      {nobody && (
        <p className="mt-2.5 text-caption leading-relaxed text-[var(--warn)]">
          当前没有任何人能浏览这个库的内容，包括你自己。你仍然可以在这里管理它。
        </p>
      )}
    </div>
  );
}

const ADMIN_OPTION_ID = "admin" as const;
type ViewerOptionId = number | typeof ADMIN_OPTION_ID;
interface ViewerOption {
  id: ViewerOptionId;
  label: string;
  hint: string;
}

/**
 * 可见成员的多选下拉：输入关键字过滤（昵称 / 用户名都匹配），列表里点选或
 * 回车切换，已选的人显示为可移除的标签。与成员管理页的选择方式一致，
 * 成员一多也不会铺成一大片按钮。
 */
function ViewerCombobox({
  options,
  selected,
  onChange,
  loading,
}: {
  options: ViewerOption[];
  selected: ViewerOptionId[];
  onChange: (next: ViewerOptionId[]) => void;
  loading: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const keyword = query.trim().toLowerCase();
  const matches = options.filter(
    (o) => keyword === "" || o.label.toLowerCase().includes(keyword) || o.hint.toLowerCase().includes(keyword),
  );
  const toggle = (id: ViewerOptionId) =>
    onChange(selected.includes(id) ? selected.filter((v) => v !== id) : [...selected, id]);
  // 点到组件外面就收起
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  useEffect(() => setActive(0), [keyword]);
  const chosen = selected
    .map((id) => options.find((o) => o.id === id))
    .filter((o): o is ViewerOption => o !== undefined);

  return (
    <div ref={rootRef} className="relative">
      <div
        className={`${INPUT_CLASS} flex min-h-[42px] flex-wrap items-center gap-1.5 !py-1.5`}
        onClick={() => rootRef.current?.querySelector("input")?.focus()}
      >
        {chosen.map((o) => (
          <span
            key={String(o.id)}
            className="flex items-center gap-1 rounded-md border border-[var(--accent)]/40 bg-[var(--accent-soft)] py-0.5 pl-2 pr-1 text-sub font-medium text-[var(--accent)]"
          >
            {o.label}
            <button
              type="button"
              aria-label={`移除 ${o.label}`}
              onClick={(e) => {
                e.stopPropagation();
                toggle(o.id);
              }}
              className="grid size-4 place-items-center rounded hover:bg-white/15"
            >
              <XIcon className="size-3" />
            </button>
          </span>
        ))}
        <input
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls="viewer-combobox-list"
          value={query}
          placeholder={chosen.length === 0 ? "输入昵称或用户名搜索，选择可浏览的人" : "继续添加…"}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return;
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setActive((i) => Math.min(i + 1, Math.max(matches.length - 1, 0)));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              if (open && matches[active]) toggle(matches[active].id);
            } else if (e.key === "Escape") {
              setOpen(false);
            } else if (e.key === "Backspace" && query === "" && chosen.length > 0) {
              toggle(chosen[chosen.length - 1].id);
            }
          }}
          className="min-w-[8rem] flex-1 bg-transparent text-ui text-[var(--text)] outline-none placeholder:text-[var(--text-faint)]"
        />
      </div>
      {open && (
        <div
          id="viewer-combobox-list"
          role="listbox"
          aria-multiselectable
          // 就地展开而不是浮层：编辑对话框的分区容器带 overflow-hidden，浮层会被裁掉
          className="mt-1.5 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.04] p-1"
        >
          {loading ? (
            <p className="px-3 py-2 text-caption text-[var(--text-faint)]">正在读取成员…</p>
          ) : matches.length === 0 ? (
            <p className="px-3 py-2 text-caption text-[var(--text-faint)]">没有匹配的成员</p>
          ) : (
            matches.map((o, index) => {
              const on = selected.includes(o.id);
              return (
                <button
                  key={String(o.id)}
                  type="button"
                  role="option"
                  aria-selected={on}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => toggle(o.id)}
                  className={`glass-row nav-item flex w-full items-center gap-2.5 px-3 py-2 text-left text-ui ${
                    index === active ? "!bg-[var(--glass-fill-hover)] !text-[var(--text)]" : ""
                  }`}
                >
                  <span
                    className={`grid size-4 shrink-0 place-items-center rounded border ${
                      on ? "border-[var(--accent)] bg-[var(--accent)] text-white" : "border-white/25"
                    }`}
                  >
                    {on && <CheckIcon className="size-3" />}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-medium">{o.label}</span>
                  <span className="shrink-0 text-caption text-[var(--text-faint)]">{o.hint}</span>
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

/** 可见范围的一句话摘要（编辑对话框的分区摘要行）。 */
function accessScopeSummary(
  mode: LibraryAccessMode,
  adminVisible: boolean,
  memberCount: number,
): string {
  if (mode === "everyone") return "所有成员";
  const parts = [adminVisible ? "我自己" : null, memberCount > 0 ? `${memberCount} 位成员` : null].filter(
    (v): v is string => v !== null,
  );
  return parts.length > 0 ? `指定成员：${parts.join(" + ")}` : "指定成员：暂无任何人可浏览";
}

/** 开关行：一句话标题 + ⓘ 长说明 + 开关；长篇解释不再常驻表单里。 */
function SwitchRow({
  title,
  detail,
  checked,
  onChange,
}: {
  title: string;
  detail: React.ReactNode;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-t border-white/[0.06] py-2.5 first:border-t-0">
      <div className="flex items-center gap-1.5 text-ui font-medium text-[var(--text)]">
        {title}
        <Tooltip content={detail} openOnClick>
          <button
            type="button"
            aria-label={`${title}的说明`}
            className="grid size-4 place-items-center rounded-full border border-white/[0.14] text-[10px] font-semibold text-[var(--text-faint)] hover:border-[var(--accent-2)] hover:text-white"
          >
            i
          </button>
        </Tooltip>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={title}
        onClick={() => onChange(!checked)}
        className="relative h-[22px] w-[38px] shrink-0 rounded-full bg-white/20 transition-colors aria-checked:bg-[var(--ok,#5fd39b)]"
      >
        <span
          className={`absolute left-[3px] top-[3px] size-4 rounded-full bg-white transition-transform ${
            checked ? "translate-x-4" : ""
          }`}
        />
      </button>
    </div>
  );
}

/** 刮削覆盖的摘要："已覆盖 3 项 · 元数据 · 命名与整理"；键按前缀归到设置页的四个分区。 */
function scrapeOverrideSummary(overrides: Record<string, unknown>): string | null {
  const keys = Object.keys(overrides);
  if (keys.length === 0) return null;
  const groups = new Set<string>();
  for (const key of keys) {
    if (key.startsWith("naming_")) groups.add("命名与整理");
    else if (key.startsWith("mirror_")) groups.add("目录写入");
    else if (/^(poster|backdrop|still)_/.test(key)) groups.add("图片");
    else groups.add("元数据");
  }
  return `已覆盖 ${keys.length} 项 · ${[...groups].join(" · ")}`;
}

function EditLibraryDialog({
  library,
  onClose,
  onSaved,
}: {
  library: MediaLibrary;
  onClose: () => void;
  onSaved: (saved: MediaLibrary) => void;
}) {
  const scraped = library.capabilities.scraped;
  const parsed = parseMatchRules(library.match_rules);
  const [name, setName] = useState(library.name);
  const [roots, setRoots] = useState<string[]>(library.root_paths);
  const [pickerTarget, setPickerTarget] = useState<"add" | number | null>(null);
  const [realtimeWatch, setRealtimeWatch] = useState(library.realtime_watch);
  const [autoClearMissing, setAutoClearMissing] = useState(library.auto_clear_missing);
  const [generateThumbnails, setGenerateThumbnails] = useState(library.generate_thumbnails);
  const [excludeFromHome, setExcludeFromHome] = useState(library.exclude_from_home);
  const [accessMode, setAccessMode] = useState<LibraryAccessMode>(library.access_mode);
  const [adminVisible, setAdminVisible] = useState(library.admin_visible);
  const [memberIds, setMemberIds] = useState<number[]>(library.member_ids);
  const [regions, setRegions] = useState<string[]>(parsed.regions);
  const [genres, setGenres] = useState<number[]>(parsed.genres);
  const [scrapeOverrides, setScrapeOverrides] = useState<Record<string, unknown>>({
    ...(library.scrape_overrides ?? {}),
  });
  // 展开的分区（同时只开一个）；默认全收起——摘要行已经把现状说清
  const [open, setOpen] = useState<EditSectionId | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const routingOptions = useRoutingOptions();

  const ready = name.trim().length > 0 && roots.length > 0;
  const missing = [name.trim() ? null : "名称", roots.length > 0 ? null : "根目录"].filter(
    (v): v is string => v !== null,
  );
  const scope = scopeSummary(library.kind, regions, genres, routingOptions);
  const scrapeText = scrapeOverrideSummary(scrapeOverrides);
  const { label: kindLabel, Icon: KindIcon } = LIBRARY_KIND_META[library.kind];

  const submit = () => {
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    const payload: LibraryPayload = {
      name: name.trim(),
      kind: library.kind,
      root_paths: roots,
      match_rules: scraped ? buildMatchRules(validGenres(library.kind, genres, routingOptions), regions) : [],
      auto_clear_missing: autoClearMissing,
      realtime_watch: realtimeWatch,
      generate_thumbnails: generateThumbnails,
      exclude_from_home: excludeFromHome,
      scrape_overrides: scraped ? scrapeOverrides : {},
      access_mode: accessMode,
      admin_visible: adminVisible,
      member_ids: memberIds,
    };
    void updateLibrary(library.id, payload)
      .then(onSaved)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const dot = (on: boolean, text: string) => (
    <span key={text} className="inline-flex items-center gap-1.5">
      <span className={`size-1.5 rounded-full ${on ? "bg-[var(--ok,#5fd39b)]" : "bg-white/25"}`} />
      {text}
    </span>
  );

  const sections: { id: EditSectionId; title: string; summary: React.ReactNode; body: React.ReactNode }[] = [
    {
      id: "basic",
      title: "基本信息",
      summary: (
        <>
          <span className="text-[var(--text)]">{name.trim() || "未命名"}</span>
          <span dir="rtl" className="min-w-0 truncate font-mono text-[var(--text-faint)]">
            {roots[0] ? "‎" + roots[0] + "‎" : "未设根目录"}
          </span>
          {roots.length > 1 && <span className="text-[var(--text-faint)]">等 {roots.length} 个目录</span>}
        </>
      ),
      body: (
        <>
          <div>
            <label className={LABEL_CLASS}>名称</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
              className={INPUT_CLASS}
            />
          </div>
          <div>
            <label className={LABEL_CLASS}>
              根目录<span className="font-normal text-[var(--text-faint)]">（第一个为主根，新内容落在这里）</span>
            </label>
            <RootsEditor roots={roots} onChange={setRoots} onPick={setPickerTarget} />
          </div>
        </>
      ),
    },
    {
      id: "scan",
      title: "扫描与监控",
      summary: (
        <>
          {dot(realtimeWatch, "实时监控")}
          {dot(autoClearMissing, "自动清理丢失")}
          {dot(generateThumbnails, scraped ? "未识别文件缩略图" : "抓帧缩略图")}
          {dot(!excludeFromHome, "首页展示")}
        </>
      ),
      body: (
        <div>
          <SwitchRow
            title="实时监控目录变化"
            checked={realtimeWatch}
            onChange={setRealtimeWatch}
            detail="新文件落盘后自动增量扫描入库。SMB/NFS 等网络挂载收不到远端变化通知、建立监听还很慢，建议关闭；关闭后由定期对账和手动扫描发现新文件，不实时但不会缺失。"
          />
          <SwitchRow
            title="扫描后自动清理丢失记录"
            checked={autoClearMissing}
            onChange={setAutoClearMissing}
            detail="自己在磁盘上删了片子后，扫描结束即把这些记录清出台账。只删记录、不动磁盘，但记录删了不可恢复——「缺失」清单里的「重新下载」也会随之消失。关闭时记录保留，文件回归自动恢复；目录读不动的那一轮不会清理。"
          />
          <SwitchRow
            title={scraped ? "为未识别文件生成缩略图" : "从视频抓帧生成缩略图"}
            checked={generateThumbnails}
            onChange={setGenerateThumbnails}
            detail="没有在线海报的内容从视频本身抓一帧当封面：优先用同名图片或内嵌封面，没有再抓帧。网络挂载库抓帧需要读取每个文件，介意流量可关闭，关闭后显示占位图。"
          />
          <SwitchRow
            title="在首页展示"
            checked={!excludeFromHome}
            onChange={(next) => setExcludeFromHome(!next)}
            detail="关闭后首页「最近添加」与 Jellyfin 客户端的「最新媒体」都跳过这个库；库卡片仍在，进库内看照常。"
          />
        </div>
      ),
    },
  ];
  if (scraped) {
    sections.push(
      {
        id: "scope",
        title: "收藏范围",
        summary: scope.declared ? (
          <span className="text-[var(--text)]">{scope.text}</span>
        ) : (
          <span className="text-[var(--text-faint)]">未声明（承接该类型未命中的作品）</span>
        ),
        body: (
          <>
            <p className="text-caption leading-relaxed text-[var(--text-faint)]">
              声明「本库收什么」，订阅与自动入库按作品特征自动选进本库；全部留空 = 不声明。
            </p>
            <ScopeEditor
              kind={library.kind}
              regions={regions}
              genres={genres}
              onRegions={setRegions}
              onGenres={setGenres}
              options={routingOptions}
            />
          </>
        ),
      },
      {
        id: "scrape",
        title: "刮削设置",
        summary: scrapeText ? (
          <span className="text-[var(--text)]">{scrapeText}</span>
        ) : (
          <span className="text-[var(--text-faint)]">跟随全局设置</span>
        ),
        body: <LibraryScrapeSettings overrides={scrapeOverrides} onChange={setScrapeOverrides} />,
      },
    );
  }
  // 可见范围放最后：它决定的是「谁能看」，与库怎么扫、收什么无关，排在配置项之后
  sections.push({
    id: "access",
    title: "可见范围",
    summary: (
      <span className={accessMode === "everyone" ? "text-[var(--text)]" : "text-[var(--warn)]"}>
        {accessScopeSummary(accessMode, adminVisible, memberIds.length)}
      </span>
    ),
    body: (
      <AccessScopeEditor
        mode={accessMode}
        adminVisible={adminVisible}
        memberIds={memberIds}
        onMode={setAccessMode}
        onAdminVisible={setAdminVisible}
        onMemberIds={setMemberIds}
      />
    ),
  });

  return (
    <>
      <Modal open onClose={onClose} label={`编辑「${library.name}」`} width="2xl" panelClassName="flex max-h-[86dvh] flex-col">
        <div className="shrink-0 border-b border-white/[0.06] px-6 pb-4 pt-5 max-md:px-5">
          <div className="flex flex-wrap items-center gap-2.5">
            <h2 className="text-title font-bold text-white">编辑「{library.name}」</h2>
            {/* 类型创建后不可改：只读展示，不再摆一排灰按钮 */}
            <span className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.08] bg-white/[0.04] py-0.5 pl-1.5 pr-2.5 text-caption text-[var(--text-muted)]">
              <KindIcon className="size-3.5 text-[var(--accent)]" />
              {kindLabel}
            </span>
          </div>
        </div>

        <div className="scroll-thin min-h-0 flex-1 space-y-2 overflow-y-auto p-6 max-md:p-5">
          {sections.map((s) => {
            const expanded = open === s.id;
            return (
              <section
                key={s.id}
                data-section={s.id}
                className="overflow-hidden rounded-[14px] border border-white/[0.08] bg-white/[0.04]"
              >
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : s.id)}
                  className="grid w-full grid-cols-[6.5rem_1fr_auto] items-center gap-3.5 px-3.5 py-3 text-left transition-colors hover:bg-white/[0.075] max-md:grid-cols-[5.5rem_1fr_auto]"
                >
                  <span className="font-semibold text-white">{s.title}</span>
                  <span className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 text-caption text-[var(--text-muted)]">
                    {s.summary}
                  </span>
                  <ChevronDownIcon
                    className={`size-3.5 text-[var(--text-faint)] transition-transform ${expanded ? "rotate-180" : ""}`}
                  />
                </button>
                {expanded && (
                  <div className="space-y-4 border-t border-white/[0.06] px-3.5 pb-4 pt-3">{s.body}</div>
                )}
              </section>
            );
          })}
        </div>

        <div className="shrink-0 border-t border-white/[0.06] px-6 py-4 max-md:px-5">
          {error && (
            <p className="mb-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-ui leading-6 text-red-200">
              {error}
            </p>
          )}
          <div className="flex items-center justify-between gap-3">
            <p className="min-w-0 truncate text-caption text-[var(--text-faint)]">
              {missing.length > 0 ? `还需填写：${missing.join("、")}` : "类型创建后不可更改"}
            </p>
            <div className="flex shrink-0 items-center gap-3">
              <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
                取消
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!ready || busy}
                className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
              >
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      </Modal>
      <RootsPicker target={pickerTarget} roots={roots} onChange={setRoots} onClose={() => setPickerTarget(null)} />
    </>
  );
}
