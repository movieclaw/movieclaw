"use client";

/**
 * 「刮削与整理」设置分区（docs/design/scrape-customization.md §3）。
 *
 * 按"配置的是什么"分四个**并列** tab：元数据（语言优先级、分级地区）、
 * 图片（海报/背景语言优先级、门槛、质量档位）、命名与整理（模板 + 实时
 * 预览）、目录写入（图片/NFO/分集剧照三项细分开关）。排列顺序沿用刮削
 * 管线的先后，只为读起来顺；**四组之间没有依赖，可任意顺序配置**——
 * 所以不编号：编号会把并列关系伪装成"必须按序完成"的向导。
 *
 * 有序优先级统一用「排序芯片」交互：点击加入优先级并按点击顺序编号
 * （首位标「主」/「首选」），再点移除；常用项直接摆在行内，长尾语种/地区
 * 经「更多」搜索面板加入（全量表来自后端代理的 TMDB configuration 接口）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
    tip: "引用「元数据」里语言优先级的第 1 位，改语言时选图自动跟随",
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
  { id: "meta", label: "元数据", detail: "语言与文本" },
  { id: "images", label: "图片", detail: "海报与背景" },
  { id: "naming", label: "命名与整理", detail: "目录与文件名" },
  { id: "mirror", label: "目录写入", detail: "NFO 与图片镜像" },
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

/* ------------------------------------------------------------------ */
/* 命名模板：实时预览                                                   */
/* ------------------------------------------------------------------ */

/**
 * 预览用的模板渲染器——**规则镜像自后端 services/library/naming.py**。
 * 权威实现在后端：保存时的校验与真实落盘的名字都由它决定，这里只为让用户
 * 边打字边看到结果。三步与后端一一对应：整组丢弃占位符全空的括号组 →
 * 替换占位符 → 收缩并清洗。改后端规则时**必须同步改这里**。
 */
const TOKEN_RE = /\{(\w+)(?::0(\d)d)?\}/g;
const BRACKET_GROUP_RE = /[([【][^()[\]【】]*[)\]】]/g;
const FORBIDDEN_RE = /[\\/:*?"<>|]/g;

function sanitizeSegment(value: string): string {
  return value
    .replace(FORBIDDEN_RE, " ")
    .replace(/\s+/g, " ")
    .replace(/^[\s.]+|[\s.]+$/g, "");
}

function tokenValue(ctx: Record<string, unknown>, name: string, pad?: string): string {
  const raw = ctx[name];
  if (raw === undefined || raw === null || raw === "") return "";
  let text = sanitizeSegment(String(raw));
  if (pad && /^\d+$/.test(text)) text = text.padStart(Number(pad), "0");
  return text;
}

function renderTemplate(template: string, ctx: Record<string, unknown>): string {
  // ① 占位符全空的括号组连同组内字面文本一起丢弃（收掉 "[tmdbid-]" 这种残留）
  const dropped = template.replace(BRACKET_GROUP_RE, (group) => {
    const tokens = [...group.matchAll(TOKEN_RE)];
    if (tokens.length === 0) return group;
    return tokens.every((m) => !tokenValue(ctx, m[1], m[2])) ? "" : group;
  });
  // ② 替换占位符
  const filled = dropped.replace(TOKEN_RE, (_m, name: string, pad?: string) =>
    tokenValue(ctx, name, pad),
  );
  // ③ 收缩：括号内侧 → 重复分隔符 → 多余空白 → 首尾分隔符
  const collapsed = filled
    .replace(/([([【])[\s\-–]+/g, "$1")
    .replace(/[\s\-–]+([)\]】])/g, "$1")
    .replace(/(?:\s*-\s*){2,}/g, " - ")
    .replace(/\s{2,}/g, " ")
    .replace(/^[\s\-–.]+|[\s\-–.]+$/g, "");
  return sanitizeSegment(collapsed) || "未命名";
}

/** 模板字段定义：可用占位符与后端 naming.ALLOWED_TOKENS 一一对应。 */
const COMMON_TOKENS = ["title", "original_title", "year", "tmdb_id", "imdb_id"];
const FILE_ATTR_TOKENS = ["resolution", "media_source", "release_group"];

const NAMING_FIELDS = [
  {
    key: "naming_entry_dir" as const,
    label: "条目目录",
    note: "电影与剧集共用",
    fallback: "{title} ({year})",
    tokens: COMMON_TOKENS,
  },
  {
    key: "naming_movie_file" as const,
    label: "电影文件名",
    note: "",
    fallback: "{title} ({year})",
    tokens: [...COMMON_TOKENS, ...FILE_ATTR_TOKENS],
  },
  {
    key: "naming_season_dir" as const,
    label: "季目录",
    note: "必须包含 {season}",
    fallback: "Season {season:02d}",
    tokens: [...COMMON_TOKENS, "season"],
  },
  {
    key: "naming_episode_file" as const,
    label: "剧集文件名",
    note: "必须包含 {season} 与 {episode}",
    fallback: "{title} ({year}) - S{season:02d}E{episode:02d}",
    tokens: [...COMMON_TOKENS, ...FILE_ATTR_TOKENS, "season", "episode", "episode_title"],
  },
];

/** 预览样例：一部电影 + 一集剧集，字段齐全便于看清每个占位符的效果。 */
const SAMPLE_MOVIE = {
  title: "沙丘：第二部",
  original_title: "Dune: Part Two",
  year: 2024,
  tmdb_id: 693134,
  imdb_id: "tt15239678",
  resolution: "2160p",
  media_source: "BluRay",
  release_group: "FRDS",
};
const SAMPLE_EPISODE = {
  title: "风筝",
  original_title: "风筝",
  year: 2017,
  tmdb_id: 68035,
  imdb_id: "tt6952510",
  season: 1,
  episode: 3,
  episode_title: "延安来的姑娘",
  resolution: "1080p",
  media_source: "WEB-DL",
  release_group: "CHDWEB",
};

/** 前端侧轻校验：与后端同口径，只为即时反馈；能否保存以后端返回为准。 */
function templateError(key: string, template: string, allowed: string[]): string | null {
  if (!template.trim()) return null; // 空 = 用默认模板
  if (/[\\/]/.test(template)) return "不能包含路径分隔符（目录层级是固定的）";
  const used = [...template.matchAll(TOKEN_RE)].map((m) => m[1]);
  const unknown = used.filter((t) => !allowed.includes(t));
  if (unknown.length) return `不可用的占位符：${unknown.map((t) => `{${t}}`).join("、")}`;
  if (
    (key === "naming_entry_dir" || key === "naming_movie_file") &&
    !used.some((t) => t === "title" || t === "original_title")
  ) {
    return "必须包含 {title} 或 {original_title}，否则不同影片会重名";
  }
  if (key === "naming_season_dir" && !used.includes("season")) {
    return "必须包含 {season}，否则不同季的同集号文件会互相覆盖";
  }
  if (key === "naming_episode_file" && !(used.includes("season") && used.includes("episode"))) {
    return "必须包含 {season} 与 {episode}，否则同一部剧的多集会互相覆盖";
  }
  return null;
}

function NamingTab({
  setting,
  patch,
}: {
  setting: ScrapeSetting;
  patch: (changes: Partial<ScrapeSetting>) => void;
}) {
  const [focused, setFocused] = useState<string>("naming_episode_file");
  const refs = useRef<Record<string, HTMLInputElement | null>>({});

  const errors = NAMING_FIELDS.map((f) => templateError(f.key, setting[f.key], f.tokens));
  const valid = errors.every((e) => e === null);

  const tpl = (key: (typeof NAMING_FIELDS)[number]["key"]) =>
    setting[key].trim() || NAMING_FIELDS.find((f) => f.key === key)!.fallback;

  const moviePath = valid
    ? `${renderTemplate(tpl("naming_entry_dir"), SAMPLE_MOVIE)}/${renderTemplate(
        tpl("naming_movie_file"),
        SAMPLE_MOVIE,
      )}.mkv`
    : "";
  const episodePath = valid
    ? `${renderTemplate(tpl("naming_entry_dir"), SAMPLE_EPISODE)}/${renderTemplate(
        tpl("naming_season_dir"),
        SAMPLE_EPISODE,
      )}/${renderTemplate(tpl("naming_episode_file"), SAMPLE_EPISODE)}.mkv`
    : "";

  const insertToken = (token: string) => {
    const key = focused;
    const field = NAMING_FIELDS.find((f) => f.key === key);
    if (!field) return;
    const input = refs.current[key];
    const current = setting[field.key] || field.fallback;
    const start = input?.selectionStart ?? current.length;
    const end = input?.selectionEnd ?? start;
    const next = `${current.slice(0, start)}{${token}}${current.slice(end)}`;
    patch({ [field.key]: next } as Partial<ScrapeSetting>);
    window.requestAnimationFrame(() => {
      const el = refs.current[key];
      if (!el) return;
      el.focus();
      const caret = start + token.length + 2;
      el.setSelectionRange(caret, caret);
    });
  };

  const focusedField = NAMING_FIELDS.find((f) => f.key === focused) ?? NAMING_FIELDS[3];

  return (
    <Card
      title="命名模板"
      perLibrary
      desc="整理与入库的目录/文件命名。留空即使用默认模板；字段缺失时会连同相邻括号自动收缩。目录层级固定为「条目目录 / 季目录 / 文件」，不可自定义。"
    >
      {NAMING_FIELDS.map((field, i) => (
        <div key={field.key} className="mt-3.5 first:mt-0">
          <label
            htmlFor={field.key}
            className="mb-1.5 flex items-baseline gap-2 text-sub font-semibold"
          >
            {field.label}
            {field.note && (
              <small className="text-caption font-normal text-[var(--text-faint)]">
                {field.note}
              </small>
            )}
          </label>
          <input
            id={field.key}
            ref={(el) => {
              refs.current[field.key] = el;
            }}
            spellCheck={false}
            placeholder={field.fallback}
            value={setting[field.key]}
            onFocus={() => setFocused(field.key)}
            onChange={(e) => patch({ [field.key]: e.target.value } as Partial<ScrapeSetting>)}
            className={`${INPUT_CLASS} w-full font-mono ${
              errors[i] ? "border-[var(--danger)]/55" : ""
            }`}
          />
          {errors[i] && <p className="mt-1.5 text-caption text-[var(--danger)]">{errors[i]}</p>}
        </div>
      ))}

      <div className="mt-4">
        <p className="mb-1.5 text-micro uppercase tracking-widest text-[var(--text-faint)]">
          可用占位符（点击插入到「{focusedField.label}」）
        </p>
        <div className="flex flex-wrap gap-1.5">
          {focusedField.tokens.map((token) => (
            <button
              key={token}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => insertToken(token)}
              className="rounded-lg border border-white/[0.08] bg-white/[0.04] px-2 py-1 font-mono text-caption text-[var(--accent-2)] transition-colors hover:bg-white/[0.07] hover:text-[var(--accent)]"
            >
              {`{${token}}`}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 overflow-hidden rounded-xl border border-white/[0.08]">
        <div className="flex items-center justify-between bg-white/[0.04] px-3.5 py-2">
          <span className="text-micro uppercase tracking-widest text-[var(--text-faint)]">
            实时预览
          </span>
          <span className={`text-caption ${valid ? "text-[var(--ok)]" : "text-[var(--danger)]"}`}>
            {valid ? "✓ 模板有效" : "✕ 模板有误"}
          </span>
        </div>
        {valid && (
          <div className="flex flex-col gap-3 overflow-x-auto px-3.5 py-3">
            <div>
              <p className="mb-1 text-caption text-[var(--text-faint)]">
                电影 · 沙丘：第二部（2024）· 2160p BluRay FRDS
              </p>
              <p className="whitespace-nowrap font-mono text-sub leading-relaxed">
                <span className="text-[var(--text-faint)]">/media/电影/</span>
                <span className="text-[var(--accent)]">{moviePath}</span>
              </p>
            </div>
            <div>
              <p className="mb-1 text-caption text-[var(--text-faint)]">
                剧集 · 风筝（2017）第 1 季第 3 集 · 1080p WEB-DL CHDWEB
              </p>
              <p className="whitespace-nowrap font-mono text-sub leading-relaxed">
                <span className="text-[var(--text-faint)]">/media/剧集/</span>
                <span className="text-[var(--accent)]">{episodePath}</span>
              </p>
            </div>
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2.5">
        <button
          type="button"
          onClick={() =>
            patch({
              naming_entry_dir: "",
              naming_movie_file: "",
              naming_season_dir: "",
              naming_episode_file: "",
            })
          }
          className="btn-glass rounded-lg px-3 py-1.5 text-sub"
        >
          恢复默认模板
        </button>
        <span className="text-caption text-[var(--text-faint)]">
          同条目多版本会自动追加「 - 版本标签」后缀，无需写进模板
        </span>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* 目录写入                                                            */
/* ------------------------------------------------------------------ */

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="relative inline-block h-[22px] w-[38px] shrink-0">
      <input
        type="checkbox"
        className="peer absolute inset-0 z-10 m-0 cursor-pointer opacity-0 disabled:cursor-default"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span
        className={`absolute inset-0 rounded-full transition-colors peer-disabled:opacity-35 ${
          checked ? "bg-[var(--accent-2)]" : "bg-white/[0.12]"
        }`}
      />
      <span
        className={`pointer-events-none absolute top-0.5 size-[18px] rounded-full bg-[#e8ecf4] transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </label>
  );
}

const MIRROR_ROWS = [
  {
    key: "mirror_images" as const,
    label: "条目图片",
    hint: "poster.jpg / fanart.jpg / 季海报",
  },
  {
    key: "mirror_nfo" as const,
    label: "NFO 元数据",
    hint: "tvshow.nfo / movie.nfo / 分集 NFO，含 tmdbid 精确身份",
  },
  {
    key: "mirror_episode_thumbs" as const,
    label: "分集剧照",
    hint: "每集一张 -thumb.jpg，长剧集写入量最大，可单独关闭",
  },
];

function MirrorTab({
  setting,
  patch,
}: {
  setting: ScrapeSetting;
  patch: (changes: Partial<ScrapeSetting>) => void;
}) {
  return (
    <Card
      title="媒体目录写入"
      perLibrary
      desc="把刮削成果写入媒体目录，反哺 Emby / Jellyfin / Kodi（文件名遵循播放器规范）。只增不删除；已存在的 NFO 绝不覆盖。每个媒体库还有一个总开关，关掉则该库三项都不写。"
    >
      <div className="divide-y divide-white/[0.06]">
        {MIRROR_ROWS.map((row) => (
          <div key={row.key} className="flex items-center justify-between gap-4 py-2.5">
            <div>
              <span className="text-ui font-medium">{row.label}</span>
              <span className="mt-0.5 block max-w-[46ch] text-caption text-[var(--text-faint)]">
                {row.hint}
              </span>
            </div>
            <Toggle
              checked={setting[row.key]}
              onChange={(next) => patch({ [row.key]: next } as Partial<ScrapeSetting>)}
            />
          </div>
        ))}
      </div>
    </Card>
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
            <span className="text-ui font-semibold">{t.label}</span>
            <span className="text-micro text-[var(--text-faint)] max-md:hidden">{t.detail}</span>
          </button>
        ))}
      </div>

      {tab === "mirror" ? (
        <MirrorTab setting={setting} patch={patch} />
      ) : tab === "naming" ? (
        <NamingTab setting={setting} patch={patch} />
      ) : tab === "meta" ? (
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
