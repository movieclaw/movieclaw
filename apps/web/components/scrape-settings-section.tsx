"use client";

/**
 * 「刮削与整理」设置分区（docs/design/scrape-customization.md §3）。
 *
 * 按刮削管线的执行顺序分 tab：STEP 1 元数据（语言优先级、分级地区）→
 * STEP 2 图片（海报/背景语言优先级、门槛、质量档位）。命名模板（STEP 3）
 * 与目录写入细分（STEP 4）随 P2/P3 接入，tab 结构已就位。
 *
 * 有序优先级统一用「排序芯片」交互：点击加入优先级并按点击顺序编号
 * （首位标「主」/「首选」），再点移除；常用项直接摆在行内，长尾语种/地区
 * 经「更多」搜索面板加入（全量表来自后端代理的 TMDB configuration 接口）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useToast } from "@/components/feedback";
import {
  type CountryOption,
  type LanguageOption,
  type ScrapeConfigView,
  type ScrapeSetting,
  getScrapeConfig,
  listCountryOptions,
  listLanguageOptions,
  saveScrapeConfig,
} from "@/lib/api/scrape";

const INPUT_CLASS =
  "rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-sub " +
  "text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
  "focus:border-[var(--accent)]/50";

/** 排序芯片的候选项。id 是落库值（语言标签 / 地区码 / meta·orig·null token）。 */
interface ChipOption {
  id: string;
  name: string;
  /** 悬停说明（特殊 token 必配，见设计文档 §3） */
  tip?: string;
}

/** 常用语种快捷芯片（元数据语言，值为带地区的语言标签）。 */
const COMMON_META_LANGS: ChipOption[] = [
  { id: "zh-CN", name: "中文（简体）" },
  { id: "zh-TW", name: "中文（繁體）" },
  { id: "en-US", name: "English" },
  { id: "ja-JP", name: "日本語" },
  { id: "ko-KR", name: "한국어" },
  { id: "fr-FR", name: "Français" },
];

/** 图片语言的特殊 token + 常用语种（值为 TMDB 图片语言码）。 */
const COMMON_IMAGE_LANGS: ChipOption[] = [
  {
    id: "meta",
    name: "跟随元数据主语言",
    tip: "引用上方元数据语言的第 1 位，改语言时选图自动跟随",
  },
  {
    id: "orig",
    name: "原始语言",
    tip: "作品的原声语言（随条目自动解析：《寄生虫》为韩语、日本动画为日语）",
  },
  { id: "en", name: "English" },
  { id: "null", name: "无文字", tip: "没有烧录任何文字的干净图（TMDB 语言标记为 null）" },
  { id: "ja", name: "日本語" },
];

const COMMON_CERT_COUNTRIES: ChipOption[] = [
  { id: "CN", name: "中国" },
  { id: "US", name: "美国" },
  { id: "JP", name: "日本" },
  { id: "GB", name: "英国" },
];

/** TMDB 图床合法档位（与后端 settings/metadata.py 的集合一致；空 = 跟随环境变量）。 */
const POSTER_SIZES = ["w342", "w500", "w780", "original"];
const BACKDROP_SIZES = ["w780", "w1280", "original"];
const STILL_SIZES = ["w185", "w300", "original"];

/* ------------------------------------------------------------------ */
/* 排序芯片                                                            */
/* ------------------------------------------------------------------ */

function OrderChips({
  options,
  extraOptions,
  moreLabel,
  value,
  max,
  primaryTag,
  onChange,
}: {
  /** 行内常用项（顺序即展示顺序）。 */
  options: ChipOption[];
  /** 「更多」搜索面板里的完整候选（可为空 = 无更多面板）。 */
  extraOptions: ChipOption[];
  moreLabel: string;
  value: string[];
  max: number;
  /** 首位角色标签（"主语言" / "首选"），空串则只显示编号。 */
  primaryTag: string;
  onChange: (next: string[]) => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const [query, setQuery] = useState("");

  // 已选中的非常用项也要出现在行内（否则保存过的长尾语种打开页面就"消失"了）
  const inline = useMemo(() => {
    const known = new Set(options.map((o) => o.id));
    const extras = value
      .filter((id) => !known.has(id))
      .map((id) => extraOptions.find((o) => o.id === id) ?? { id, name: id });
    return [...options, ...extras];
  }, [options, extraOptions, value]);

  const toggle = useCallback(
    (id: string) => {
      const index = value.indexOf(id);
      if (index >= 0) {
        if (value.length <= 1) return; // 至少保留一项
        onChange(value.filter((v) => v !== id));
      } else {
        if (value.length >= max) return;
        onChange([...value, id]);
      }
    },
    [value, max, onChange],
  );

  const filteredExtra = useMemo(() => {
    const inlineIds = new Set(inline.map((o) => o.id));
    const q = query.trim().toLowerCase();
    return extraOptions
      .filter((o) => !inlineIds.has(o.id))
      .filter((o) => !q || o.name.toLowerCase().includes(q) || o.id.toLowerCase().includes(q))
      .slice(0, 60);
  }, [extraOptions, inline, query]);

  const summary = value
    .map((id, i) => {
      const name = inline.find((o) => o.id === id)?.name ?? id;
      return i === 0 && primaryTag ? `${primaryTag} ${name}` : name;
    })
    .join(" → ");

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {inline.map((option) => {
          const index = value.indexOf(option.id);
          const selected = index >= 0;
          const disabled = !selected && value.length >= max;
          return (
            <button
              key={option.id}
              type="button"
              title={option.tip}
              aria-pressed={selected}
              disabled={disabled}
              onClick={() => toggle(option.id)}
              className={`flex items-center gap-1.5 rounded-full border py-1.5 pl-1.5 pr-3 text-sub font-medium transition-colors ${
                selected
                  ? "border-[var(--accent-2)] bg-[var(--accent-soft)] text-[var(--text)]"
                  : "border-white/[0.08] bg-white/[0.04] text-[var(--text-muted)] hover:bg-white/[0.07]"
              } ${disabled ? "pointer-events-none opacity-35" : ""}`}
            >
              <span
                className={`flex size-4.5 items-center justify-center rounded-full text-micro font-bold tabular-nums ${
                  selected
                    ? "bg-[var(--accent)] text-[#12141c]"
                    : "bg-white/10 text-[var(--text-faint)]"
                }`}
              >
                {selected ? index + 1 : "+"}
              </span>
              {option.name}
            </button>
          );
        })}
        {extraOptions.length > 0 && (
          <button
            type="button"
            onClick={() => setMoreOpen((open) => !open)}
            className="flex items-center gap-1.5 rounded-full border border-dashed border-white/[0.15] bg-white/[0.02] py-1.5 pl-1.5 pr-3 text-sub text-[var(--text-faint)] transition-colors hover:text-[var(--text)]"
          >
            <span className="flex size-4.5 items-center justify-center rounded-full bg-white/10 text-micro">
              …
            </span>
            更多{moreLabel}
          </button>
        )}
      </div>
      {moreOpen && (
        <div className="mt-2.5 rounded-xl border border-white/[0.12] bg-white/[0.04] p-3">
          <input
            className={`${INPUT_CLASS} mb-2.5 w-full`}
            placeholder={`搜索${moreLabel}（名称或代码）…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="flex max-h-44 flex-wrap content-start gap-1.5 overflow-y-auto">
            {filteredExtra.length === 0 ? (
              <span className="px-1 text-caption text-[var(--text-faint)]">
                没有匹配的{moreLabel}
              </span>
            ) : (
              filteredExtra.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => {
                    toggle(option.id);
                    setMoreOpen(false);
                    setQuery("");
                  }}
                  className="rounded-full border border-white/[0.08] bg-white/[0.04] px-3 py-1 text-sub text-[var(--text-muted)] transition-colors hover:bg-white/[0.07] hover:text-[var(--text)]"
                >
                  {option.name}
                  <span className="ml-1.5 font-mono text-micro text-[var(--text-faint)]">
                    {option.id}
                  </span>
                </button>
              ))
            )}
          </div>
        </div>
      )}
      <p className="mt-2 text-caption text-[var(--text-faint)]">
        {value.length
          ? `当前顺序：${summary}（点击已选项可移除，最多 ${max} 项）`
          : "点击选择，按点击顺序排列优先级"}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 分区主体                                                            */
/* ------------------------------------------------------------------ */

const TABS = [
  { id: "meta", step: "STEP 1", label: "元数据", detail: "语言与文本" },
  { id: "images", step: "STEP 2", label: "图片", detail: "海报与背景" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function Card({
  title,
  perLibrary,
  desc,
  children,
}: {
  title: string;
  perLibrary?: boolean;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-white/[0.08] bg-white/[0.04] p-5">
      <div className="flex items-center gap-2.5">
        <h3 className="text-title-sm font-semibold">{title}</h3>
        {perLibrary && (
          <span className="rounded-full border border-white/[0.15] px-2 py-px text-micro text-[var(--accent-2)]">
            可按库覆盖
          </span>
        )}
      </div>
      <p className="mb-4 mt-1 max-w-[62ch] text-sub text-[var(--text-muted)]">{desc}</p>
      {children}
    </section>
  );
}

export function ScrapeSettingsSection() {
  const toast = useToast();
  const [view, setView] = useState<ScrapeConfigView | null>(null);
  const [setting, setSetting] = useState<ScrapeSetting | null>(null);
  const [languages, setLanguages] = useState<LanguageOption[]>([]);
  const [countries, setCountries] = useState<CountryOption[]>([]);
  const [tab, setTab] = useState<TabId>("meta");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const config = await getScrapeConfig();
      setView(config);
      // 编辑态用"生效值"起步：跟随 env 的空列表在界面上就是当前生效的语言，
      // 用户看到的即所得；未改动不保存则语义不变
      setSetting({
        ...config.setting,
        language_priority: config.setting.language_priority.length
          ? config.setting.language_priority
          : config.effective.language_priority,
      });
      setDirty(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败，请重试");
    }
    // 全量表拉不到不阻断（面板回落只显示常用项）
    listLanguageOptions().then(setLanguages).catch(() => {});
    listCountryOptions().then(setCountries).catch(() => {});
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = useCallback((changes: Partial<ScrapeSetting>) => {
    setSetting((current) => (current ? { ...current, ...changes } : current));
    setDirty(true);
  }, []);

  const save = useCallback(async () => {
    if (!setting) return;
    setSaving(true);
    try {
      const config = await saveScrapeConfig(setting);
      setView(config);
      setSetting(config.setting);
      setDirty(false);
      toast.success("已保存。语言与图片对存量条目生效需在媒体库执行整库刷新");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  }, [setting, toast]);

  const extraMetaLangs = useMemo<ChipOption[]>(
    () =>
      languages
        .filter((l) => !COMMON_META_LANGS.some((c) => c.id.startsWith(`${l.code}-`)))
        .map((l) => ({ id: l.code, name: l.name || l.english_name || l.code })),
    [languages],
  );
  const extraImageLangs = useMemo<ChipOption[]>(
    () =>
      languages
        .filter((l) => !COMMON_IMAGE_LANGS.some((c) => c.id === l.code))
        .map((l) => ({ id: l.code, name: l.name || l.english_name || l.code })),
    [languages],
  );
  const extraCountries = useMemo<ChipOption[]>(
    () =>
      countries
        .filter((c) => !COMMON_CERT_COUNTRIES.some((k) => k.id === c.code))
        .map((c) => ({ id: c.code, name: c.name })),
    [countries],
  );

  if (error) {
    return (
      <div className="rounded-xl bg-white/[0.03] px-4 py-6 text-center text-ui text-[var(--text-muted)]">
        {error}
        <button type="button" className="btn-glass ml-3 px-3 py-1.5 text-sub" onClick={load}>
          重试
        </button>
      </div>
    );
  }
  if (!setting) {
    return (
      <p className="flex items-center gap-2.5 rounded-xl bg-white/[0.03] px-4 py-5 text-ui text-[var(--text-muted)]">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在加载刮削配置…
      </p>
    );
  }

  const sizeHint = (value: string, effective: string) =>
    value === "" ? `跟随环境变量（当前 ${effective}）` : value;

  return (
    <div className="space-y-5">
      {/* tab 栏：编号即刮削管线的执行顺序 */}
      <div className="flex gap-1.5 rounded-2xl border border-white/[0.08] bg-white/[0.03] p-1.5">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`flex flex-1 flex-col items-center gap-px rounded-xl px-3 py-2 transition-colors ${
              tab === t.id
                ? "bg-white/[0.12] text-[var(--text)]"
                : "text-[var(--text-muted)] hover:bg-white/[0.06]"
            }`}
          >
            <span
              className={`text-micro tabular-nums tracking-widest ${
                tab === t.id ? "text-[var(--accent-2)]" : "text-[var(--text-faint)]"
              }`}
            >
              {t.step}
            </span>
            <span className="text-ui font-semibold">{t.label}</span>
            <span className="text-micro text-[var(--text-faint)] max-md:hidden">{t.detail}</span>
          </button>
        ))}
      </div>

      {tab === "meta" ? (
        <>
          <Card
            title="元数据语言"
            desc="标题、简介、类型名等文本的语言。点选语言即加入优先级，第 1 位是主语言（决定向 TMDB 请求的语言），缺失的字段按顺序回落——回落基于已拉取的翻译数据，不产生额外请求。"
          >
            <OrderChips
              options={COMMON_META_LANGS}
              extraOptions={extraMetaLangs}
              moreLabel="语言"
              value={setting.language_priority}
              max={3}
              primaryTag="主语言"
              onChange={(next) => patch({ language_priority: next })}
            />
          </Card>
          <Card
            title="内容分级"
            desc="条目分级（如 PG-13、TV-MA）按顺序取第一个有数据的地区。"
          >
            <OrderChips
              options={COMMON_CERT_COUNTRIES}
              extraOptions={extraCountries}
              moreLabel="地区"
              value={setting.cert_country_priority}
              max={6}
              primaryTag=""
              onChange={(next) => patch({ cert_country_priority: next })}
            />
          </Card>
        </>
      ) : (
        <>
          <Card
            title="海报"
            perLibrary
            desc="海报和文本一样有语言：中文版、原版、无文字干净版是不同的候选图。你在条目详情页手动选定的图始终优先，不受这里影响。"
          >
            <div className="grid gap-2 md:grid-cols-2">
              {(
                [
                  {
                    id: "default",
                    title: "TMDB 默认",
                    desc: "与发现页看到的一致，订阅前后海报不跳变（默认）",
                  },
                  {
                    id: "language",
                    title: "按语言优先级挑选",
                    desc: "逐级取第一档有候选图的语言，档内按分辨率与票数排序",
                  },
                ] as const
              ).map((mode) => (
                <label
                  key={mode.id}
                  className={`cursor-pointer rounded-xl border p-3 transition-colors ${
                    setting.poster_mode === mode.id
                      ? "border-[var(--accent-2)] bg-[var(--accent-soft)]"
                      : "border-white/[0.08] bg-white/[0.04] hover:bg-white/[0.07]"
                  }`}
                >
                  <input
                    type="radio"
                    name="poster-mode"
                    className="sr-only"
                    checked={setting.poster_mode === mode.id}
                    onChange={() => patch({ poster_mode: mode.id })}
                  />
                  <span className="block text-ui font-semibold">{mode.title}</span>
                  <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                    {mode.desc}
                  </span>
                </label>
              ))}
            </div>
            <div
              className={`mt-4 ${setting.poster_mode === "language" ? "" : "pointer-events-none opacity-40"}`}
            >
              <OrderChips
                options={COMMON_IMAGE_LANGS}
                extraOptions={extraImageLangs}
                moreLabel="语言"
                value={setting.poster_language_priority}
                max={4}
                primaryTag="首选"
                onChange={(next) => patch({ poster_language_priority: next })}
              />
            </div>
          </Card>
          <Card
            title="背景图（fanart）"
            perLibrary
            desc="铺在详情页全屏的沉浸底图。「无文字」是没有烧录任何片名文字的干净图——排第 1 位即无文字优先；想要带片名 logo 的横图，把语言排到前面。"
          >
            <OrderChips
              options={COMMON_IMAGE_LANGS}
              extraOptions={extraImageLangs}
              moreLabel="语言"
              value={setting.backdrop_language_priority}
              max={4}
              primaryTag="首选"
              onChange={(next) => patch({ backdrop_language_priority: next })}
            />
          </Card>
          <Card
            title="质量与门槛"
            desc="分辨率门槛过滤模糊候选图；质量档位决定下载到本地的图片尺寸，调低可显著节省磁盘，改动后整库刷新会按新档位自动重下。"
          >
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] pb-3">
              <div>
                <span className="text-ui font-medium">最低分辨率门槛</span>
                <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                  低于门槛的候选图不选（0 = 不限制）；候选全部不达标时自动放宽
                </span>
              </div>
              <div className="flex items-center gap-2.5 text-sub text-[var(--text-muted)]">
                海报 ≥
                <input
                  type="number"
                  min={0}
                  step={100}
                  className={`${INPUT_CLASS} w-24 tabular-nums`}
                  value={setting.poster_min_width}
                  onChange={(e) => patch({ poster_min_width: Number(e.target.value) || 0 })}
                />
                背景 ≥
                <input
                  type="number"
                  min={0}
                  step={100}
                  className={`${INPUT_CLASS} w-24 tabular-nums`}
                  value={setting.backdrop_min_width}
                  onChange={(e) => patch({ backdrop_min_width: Number(e.target.value) || 0 })}
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
              <span className="text-ui font-medium">图片质量档位</span>
              <div className="flex flex-wrap gap-2">
                {(
                  [
                    ["poster_size", "海报", POSTER_SIZES, view?.effective.poster_size],
                    ["backdrop_size", "背景", BACKDROP_SIZES, view?.effective.backdrop_size],
                    ["still_size", "剧照", STILL_SIZES, view?.effective.still_size],
                  ] as const
                ).map(([key, label, sizes, effective]) => (
                  <select
                    key={key}
                    className={INPUT_CLASS}
                    value={setting[key]}
                    title={sizeHint(setting[key], effective ?? "")}
                    onChange={(e) => patch({ [key]: e.target.value } as Partial<ScrapeSetting>)}
                  >
                    <option value="">
                      {label} · 跟随环境（{effective}）
                    </option>
                    {sizes.map((size) => (
                      <option key={size} value={size}>
                        {label} · {size}
                      </option>
                    ))}
                  </select>
                ))}
              </div>
            </div>
          </Card>
        </>
      )}

      <div className="flex items-center justify-between gap-3">
        <p className="text-caption text-[var(--text-faint)]">
          保存后对新刮削立即生效；存量条目在媒体库页执行「刷新元数据」后按新配置更新。
        </p>
        <button
          type="button"
          disabled={!dirty || saving}
          onClick={save}
          className="btn-accent shrink-0 rounded-full px-5 py-2 text-ui font-semibold disabled:opacity-50"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
