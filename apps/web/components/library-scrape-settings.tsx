"use client";

/**
 * 「编辑库 → 刮削设置」页签（docs/design/scrape-customization.md §14.5）。
 *
 * 与全局设置页**共用同一套控件**（`scrape-settings-section.tsx` 导出的四组
 * 字段），差别只在外面包了一层三态壳：每个字段要么跟随全局，要么显式覆盖。
 * 用户在这里学到的交互与设置页完全一致，只多了「跟随全局 ⇄ 自定义」这一个概念。
 *
 * 三态怎么表达（设计文档 §14.5 的表）：
 * - 排序芯片与数字输入没有"空值"可表达跟随，用**卡片级**开关（由 Card 渲染）：
 *   切到自定义时拿全局当前值做种子，用户在这个基础上改；切回跟随即把这张卡的
 *   键从覆盖里删掉。粒度卡在"卡片"这一层是两头夹出来的——再粗（整个 tab 一个
 *   开关）会让只覆盖了语言的库把分级也显示成已自定义，状态是错的；再细（逐字段）
 *   对有序列表没有意义，排序芯片只能整张卡一起跟随或一起自定义；
 * - 命名模板留空即跟随（沿用 P3 的 placeholder 机制）；
 * - 目录写入是「跟随全局 / 开 / 关」三档按钮（同样沿用 P3）。
 *
 * 覆盖对象的语义与后端一致：**只存显式覆盖的字段**，空对象 = 全跟全局。
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ImagesTab,
  MetaTab,
  useScrapeChipOptions,
} from "@/components/scrape-settings-section";
import {
  type ScrapeConfigView,
  type ScrapeSetting,
  getScrapeConfig,
} from "@/lib/api/scrape";

/** 页签内的分组：与全局设置页的四个 tab 一一对应。 */
const GROUPS = [
  { id: "meta", label: "元数据", detail: "语言与分级" },
  { id: "images", label: "图片", detail: "海报与背景" },
  { id: "naming", label: "命名与整理", detail: "目录与文件名" },
  { id: "mirror", label: "目录写入", detail: "NFO 与图片镜像" },
] as const;

type GroupId = (typeof GROUPS)[number]["id"];

/** 各分组管辖的键，只用来给页签点小圆点（"这组设过没有"）。
 *  三态开关本身是**卡片级**的，由 Card 自己渲染（见 followFor）。 */
const GROUP_KEYS: Record<GroupId, string[]> = {
  meta: ["language_priority", "cert_country_priority"],
  images: [
    "poster_mode",
    "poster_language_priority",
    "backdrop_language_priority",
    "poster_min_width",
    "backdrop_min_width",
    "poster_size",
    "backdrop_size",
    "still_size",
  ],
  naming: ["naming_entry_dir", "naming_movie_file", "naming_season_dir", "naming_episode_file"],
  mirror: ["mirror_images", "mirror_nfo", "mirror_episode_thumbs"],
};

const NAMING_FIELDS = [
  { key: "naming_entry_dir", label: "条目目录", fallback: "{title} ({year})" },
  { key: "naming_movie_file", label: "电影文件名", fallback: "{title} ({year})" },
  { key: "naming_season_dir", label: "季目录", fallback: "Season {season:02d}" },
  {
    key: "naming_episode_file",
    label: "剧集文件名",
    fallback: "{title} ({year}) - S{season:02d}E{episode:02d}",
  },
] as const;

const MIRROR_FIELDS = [
  { key: "mirror_images", label: "写入条目图片" },
  { key: "mirror_nfo", label: "写入 NFO 元数据" },
  { key: "mirror_episode_thumbs", label: "写入分集剧照" },
] as const;

/** 全局值的可读摘要，供卡片里的「全局：…」对照行显示。 */
function summarize(keys: (keyof ScrapeSetting)[], base: ScrapeSetting): string {
  return keys
    .map((key) => {
      const value = base[key];
      if (Array.isArray(value)) return value.join(" → ");
      if (typeof value === "boolean") return value ? "开" : "关";
      return String(value || "跟随环境");
    })
    .filter(Boolean)
    .slice(0, 3)
    .join("；");
}

export function LibraryScrapeSettings({
  overrides,
  onChange,
}: {
  overrides: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const [group, setGroup] = useState<GroupId>("meta");
  const [config, setConfig] = useState<ScrapeConfigView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chipOptions = useScrapeChipOptions();

  useEffect(() => {
    getScrapeConfig()
      .then(setConfig)
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "全局刮削设置加载失败"),
      );
  }, []);

  /** 全局基线：与设置页同一套"编辑态用生效值起步"的口径。 */
  const base = useMemo<ScrapeSetting | null>(() => {
    if (!config) return null;
    return {
      ...config.setting,
      language_priority: config.setting.language_priority.length
        ? config.setting.language_priority
        : config.effective.language_priority,
    };
  }, [config]);

  /** 交给共用控件渲染的完整设置 = 全局基线叠加本库覆盖。 */
  const merged = useMemo<ScrapeSetting | null>(
    () => (base ? { ...base, ...(overrides as Partial<ScrapeSetting>) } : null),
    [base, overrides],
  );

  const patch = useCallback(
    (changes: Partial<ScrapeSetting>) => onChange({ ...overrides, ...changes }),
    [overrides, onChange],
  );

  const setField = useCallback(
    (key: string, value: unknown) => {
      const next = { ...overrides };
      if (value === undefined) delete next[key];
      else next[key] = value;
      onChange(next);
    },
    [overrides, onChange],
  );

  /** 卡片的跟随/自定义状态与切换：自定义时用全局值做种子，跟随时删掉这张卡的键。 */
  const followFor = useCallback(
    (keys: (keyof ScrapeSetting)[]) => ({
      custom: keys.some((key) => key in overrides),
      globalSummary: base ? summarize(keys, base) : "",
      onToggle: (custom: boolean) => {
        if (!base) return;
        const next = { ...overrides };
        for (const key of keys) {
          if (custom) next[key] = base[key];
          else delete next[key];
        }
        onChange(next);
      },
    }),
    [base, overrides, onChange],
  );

  if (error) {
    return <p className="text-sub text-[var(--text-muted)]">{error}</p>;
  }
  if (!merged || !base) {
    return (
      <p className="flex items-center gap-2.5 text-sub text-[var(--text-muted)]">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在加载全局刮削设置…
      </p>
    );
  }

  const overrideCount = Object.keys(overrides).length;

  return (
    <div className="space-y-4">
      <p className="text-sub leading-relaxed text-[var(--text-muted)]">
        本库单独的刮削口味，未显式修改的项跟随「设置 → 刮削与整理」。
        {overrideCount > 0 && (
          <span className="ml-1.5 rounded-full bg-[var(--accent-soft)] px-2 py-px text-micro text-[var(--accent)]">
            已覆盖 {overrideCount} 项
          </span>
        )}
      </p>

      <div className="flex gap-1.5 rounded-2xl border border-white/[0.08] bg-white/[0.03] p-1.5">
        {GROUPS.map((g) => {
          const dirty = GROUP_KEYS[g.id].some((key) => key in overrides);
          return (
            <button
              key={g.id}
              type="button"
              onClick={() => setGroup(g.id)}
              className={`flex flex-1 flex-col items-center gap-px rounded-xl px-2 py-2 transition-colors ${
                group === g.id
                  ? "bg-white/[0.12] text-[var(--text)]"
                  : "text-[var(--text-muted)] hover:bg-white/[0.06]"
              }`}
            >
              <span className="flex items-center gap-1 text-sub font-semibold">
                {g.label}
                {dirty && (
                  <span className="size-1.5 rounded-full bg-[var(--accent)]" aria-hidden />
                )}
              </span>
              <span className="text-micro text-[var(--text-faint)] max-md:hidden">{g.detail}</span>
            </button>
          );
        })}
      </div>

      {group === "meta" && (
        <MetaTab
          setting={merged}
          patch={patch}
          extraMetaLangs={chipOptions.metaLangs}
          extraCountries={chipOptions.certCountries}
          followFor={followFor}
        />
      )}
      {group === "images" && (
        <ImagesTab
          setting={merged}
          patch={patch}
          extraImageLangs={chipOptions.imageLangs}
          effective={config?.effective ?? null}
          followFor={followFor}
        />
      )}

      {group === "naming" && (
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.04] p-4">
          <p className="mb-3 text-caption leading-relaxed text-[var(--text-faint)]">
            留空即跟随全局模板。命名的产物是本库目录树里的路径，所以每个库可以各用一套。
          </p>
          {NAMING_FIELDS.map((field) => (
            <div key={field.key} className="mb-2.5 last:mb-0">
              <label className="mb-1 block text-caption text-[var(--text-muted)]">
                {field.label}
              </label>
              <input
                spellCheck={false}
                placeholder={`跟随全局（${base[field.key] || field.fallback}）`}
                value={(overrides[field.key] as string) ?? ""}
                onChange={(e) =>
                  setField(field.key, e.target.value.trim() ? e.target.value : undefined)
                }
                className="w-full rounded-lg border border-white/[0.08] bg-white/[0.04] px-2.5 py-1.5 font-mono text-sub text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
              />
            </div>
          ))}
        </div>
      )}

      {group === "mirror" && (
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.04] p-4">
          <p className="mb-1 text-caption leading-relaxed text-[var(--text-faint)]">
            把刮削成果写入本库的媒体目录。基本信息里的「刮削图片/NFO 写入媒体目录」是总闸，
            关掉则这三项全不写。
          </p>
          {MIRROR_FIELDS.map((field) => {
            const value = overrides[field.key] as boolean | undefined;
            return (
              <div
                key={field.key}
                className="flex items-center justify-between gap-3 border-t border-white/[0.05] py-2.5"
              >
                <span className="text-sub">{field.label}</span>
                <div className="flex gap-1">
                  {(
                    [
                      [undefined, `跟随全局（${base[field.key] ? "开" : "关"}）`],
                      [true, "开"],
                      [false, "关"],
                    ] as const
                  ).map(([option, label]) => (
                    <button
                      key={String(option)}
                      type="button"
                      onClick={() => setField(field.key, option)}
                      className={`rounded-lg px-2.5 py-1 text-caption transition-colors ${
                        value === option
                          ? "bg-[var(--accent-soft)] text-[var(--text)]"
                          : "text-[var(--text-faint)] hover:bg-white/[0.06]"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="text-caption leading-relaxed text-[var(--text-faint)]">
        语言与选图的产物挂在条目上（一部片一份档案、一张海报），所以它们按条目的
        <strong className="font-medium text-[var(--text-muted)]">刮削归属库</strong>
        生效——归属本库的条目才跟这里的设置。存量条目需在本库执行「刷新元数据」后按新设置重刮。
      </p>
    </div>
  );
}
