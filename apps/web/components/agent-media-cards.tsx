"use client";

import { memo, useEffect, useMemo, useState, type ReactNode } from "react";

import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { HScroller } from "@/components/h-scroller";
import { FilmIcon, PlayIcon, TvIcon } from "@/components/icons";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { parseMediaCardsArgs, type MediaCardGroup, type MediaCardSpec } from "@/lib/agent-media-cards";
import type { AgentProcessItem, AgentTurnSegment } from "@/lib/agent-conversations";
import { fetchDiscoveredTitleDetails } from "@/lib/api/discover";
import {
  getLibrary,
  getLibraryItemDetail,
  type LibraryItemDetail,
  type MediaLibrary,
} from "@/lib/api/libraries";
import {
  fetchResumeState,
  getPlaybackItem,
  type PlaybackItemInfo,
  type PlaybackWatchState,
} from "@/lib/api/playback";
import { publicEnv } from "@/lib/env";
import { formatBytes } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import type { MediaItem, MediaType } from "@/lib/media-types";
import { playHref, rememberPlayerReturnPath } from "@/lib/player/play-links";
import { useTapGuard } from "@/lib/use-tap-guard";

/**
 * Agent 生成式 UI 的渲染端（docs/design/agent-generative-ui.md）。
 *
 * 模型调用 render_media_cards_v1 时只给编号；这里按编号调产品既有接口现取数据，
 * 复用全站同款的海报卡、库封面拼贴与播放卡视觉，卡片因此与发现页/媒体库页
 * 长得一模一样，订阅、入库、观看进度也永远是此刻的真实状态。
 *
 * 加载态与失败态都占同样的盒子：卡片组出现在会话时间线中间，尺寸抖动会把
 * 正在阅读的正文推来推去。编号查不到（模型编错了 / 条目已删）显示「未找到」
 * 而不是整组消失——用户要能看出模型引用了不存在的东西。
 */

/** 媒体类型的图标与文案（与媒体库页同款，此处内联避免把整个 library-view 拉进会话页）。 */
const KIND_META: Record<MediaType, { label: string; Icon: typeof FilmIcon }> = {
  movie: { label: "电影", Icon: FilmIcon },
  tv: { label: "剧集", Icon: TvIcon },
};

type Loaded<T> = { status: "loading" } | { status: "ready"; data: T } | { status: "error" };

/**
 * 卡片数据加载：一次性、按 key 触发。失败不区分原因——404（编号不存在）与
 * 网络错误在卡片上都是同一个「未找到/加载失败」占位，用户下一步都是重问。
 */
function useCardData<T>(key: string, load: () => Promise<T>): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    load().then(
      (data) => {
        if (!cancelled) setState({ status: "ready", data });
      },
      () => {
        if (!cancelled) setState({ status: "error" });
      },
    );
    return () => {
      cancelled = true;
    };
    // key 即数据身份：spec 对象在每次流式刷新时可能是新引用，但编号不变就不重拉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

/** 加载/失败占位：与真实卡片同尺寸，中央一行小字。 */
function CardPlaceholder({
  className,
  children,
  failed,
}: {
  className: string;
  children?: ReactNode;
  failed?: boolean;
}) {
  return (
    <div
      className={`flex items-center justify-center rounded-2xl bg-white/[0.04] ring-1 ring-white/[0.06] ${
        failed ? "" : "animate-pulse"
      } ${className}`}
    >
      {children ?? (
        <span className="px-3 text-center text-caption text-[var(--text-faint)]">
          {failed ? "未找到" : ""}
        </span>
      )}
    </div>
  );
}

/* —— 媒体库卡片：封面拼贴 + 库名 + 类型与库存统计 —— */

const LibraryMiniCard = memo(function LibraryMiniCard({ libraryId }: { libraryId: number }) {
  const state = useCardData(`library:${libraryId}`, () => getLibrary(libraryId));
  const box = "w-[248px] shrink-0 max-md:w-[212px]";
  if (state.status !== "ready") {
    return (
      <div className={box}>
        <CardPlaceholder className="aspect-[21/10] w-full" failed={state.status === "error"}>
          {state.status === "error" ? (
            <span className="text-caption text-[var(--text-faint)]">未找到媒体库 #{libraryId}</span>
          ) : null}
        </CardPlaceholder>
      </div>
    );
  }
  return <LibraryMiniCardBody library={state.data} />;
});

function LibraryMiniCardBody({ library }: { library: MediaLibrary }) {
  const meta = KIND_META[library.kind];
  // 服务端渲染的封面拼贴（与媒体库首页同一张图）；空库/生成失败退回类型图标底纹
  const [coverFailed, setCoverFailed] = useState(false);
  const { stats } = library;
  const summary = [
    meta.label,
    `${stats.item_count} 部`,
    stats.total_size_bytes > 0 ? formatBytes(stats.total_size_bytes) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Link
      href={`/library/${library.id}` as Route}
      scroll={false}
      aria-label={`打开媒体库「${library.name}」，${summary}`}
      className="group/lib block w-[248px] shrink-0 outline-none max-md:w-[212px]"
    >
      <div className="relative aspect-[21/10] overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.38)] ring-1 ring-white/10 transition duration-300 group-hover/lib:-translate-y-1 group-hover/lib:ring-white/35 group-focus-visible/lib:ring-2 group-focus-visible/lib:ring-[var(--accent-ring)]">
        {coverFailed || stats.item_count === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-[#1c2230] to-[#10131c]">
            <meta.Icon className="size-10 text-white/[0.13]" />
          </div>
        ) : (
          <img
            src={`${publicEnv.apiBaseUrl}/libraries/${library.id}/cover`}
            alt=""
            loading="lazy"
            className="absolute inset-0 size-full object-cover transition duration-300 group-hover/lib:scale-[1.02]"
            onError={() => setCoverFailed(true)}
          />
        )}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-14 bg-gradient-to-t from-black/70 to-transparent" />
        <span className="absolute left-2 top-2 flex items-center gap-1 rounded-full bg-black/55 px-2 py-0.5 text-micro font-semibold text-white/85">
          <meta.Icon className="size-3" />
          {meta.label}
        </span>
        {library.is_default && (
          <span className="absolute right-2 top-2 rounded-full border border-white/[0.14] bg-white/[0.12] px-2 py-0.5 text-micro font-semibold text-white/85">
            默认
          </span>
        )}
        <div className="absolute inset-x-3 bottom-2.5">
          <p className="text-on-image truncate text-ui font-semibold text-white">{library.name}</p>
        </div>
      </div>
      <p className="tnum mt-1.5 truncate px-0.5 text-caption text-[var(--text-muted)]">{summary}</p>
    </Link>
  );
}

/* —— 影片海报卡片：与发现页同款，含已入库/已订阅斜标与悬停订阅键 —— */

const TitlePosterCard = memo(function TitlePosterCard({
  spec,
}: {
  spec: Extract<MediaCardSpec, { kind: "title" }>;
}) {
  const state = useCardData(spec.key, () =>
    fetchDiscoveredTitleDetails(spec.titleRef).then((detail) => detail.item),
  );
  const box = "w-[152px] shrink-0 max-md:w-[126px]";
  if (state.status !== "ready") {
    return (
      <div className={box}>
        <CardPlaceholder className="aspect-[2/3] w-full" failed={state.status === "error"} />
      </div>
    );
  }
  return (
    <div className={box}>
      <TitlePosterCardBody item={state.data} />
    </div>
  );
});

function TitlePosterCardBody({ item }: { item: MediaItem }) {
  const { subscriptionOf } = useSubscribeEntry();
  // 海报斜标：已入库（绿）优先于已订阅（蓝）——在库是更确定的事实；
  // 悬停信息层里的订阅键由 PosterCardVisual 自己按订阅状态切换文案
  const visual: PosterVisualItem = useMemo(() => {
    const subscribed = Boolean(subscriptionOf(item));
    return subscribed && !item.libraryStatus
      ? { ...item, ribbon: "已订阅", ribbonVariant: "compact-left", ribbonTone: "subscribed" }
      : item;
  }, [item, subscriptionOf]);
  const href = (
    item.source === "douban" ? `/media/douban/${item.id}` : `/media/${item.type}/${item.id}`
  ) as Route;
  return <PosterCardVisual item={visual} href={href} action="subscribe" revealInfoOnTouch />;
}

/* —— 库内条目播放卡片：剧照/海报 + 一键播放 + 观看进度 + 片源规格 —— */

interface LibraryItemCardData {
  info: PlaybackItemInfo;
  detail: LibraryItemDetail | null;
  watch: PlaybackWatchState | null;
}

const LibraryItemPlayCard = memo(function LibraryItemPlayCard({
  spec,
}: {
  spec: Extract<MediaCardSpec, { kind: "library_item" }>;
}) {
  const state = useCardData(spec.key, async (): Promise<LibraryItemCardData> => {
    // 条目页信息只认 media_item_id（库归属服务端按可见性解析）；拿到库 id 后
    // 再并行取详情（剧照/规格）与观看状态。后两者失败不拖垮卡片——没有
    // 剧照就用海报铺底，没有进度就不画进度条
    const info = await getPlaybackItem(spec.mediaItemId);
    const [detail, watch] = await Promise.all([
      getLibraryItemDetail(info.library_id, info.media_item_id).catch(() => null),
      fetchResumeState({
        media_item_id: info.media_item_id,
        season_number: spec.season,
        episode_number: spec.episode,
      }).catch(() => null),
    ]);
    return { info, detail, watch };
  });
  const box = "w-[248px] shrink-0 max-md:w-[212px]";
  if (state.status !== "ready") {
    return (
      <div className={box}>
        <CardPlaceholder className="aspect-video w-full" failed={state.status === "error"}>
          {state.status === "error" ? (
            <span className="text-caption text-[var(--text-faint)]">
              未找到条目 #{spec.mediaItemId}
            </span>
          ) : null}
        </CardPlaceholder>
      </div>
    );
  }
  return <LibraryItemPlayCardBody data={state.data} spec={spec} />;
});

/** S1E3 → S01E03（与播放器/最近观看同一写法）。 */
function episodeCode(season: number, episode: number): string {
  return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
}

/** 片源规格一行：去重的分辨率/HDR 标签 + 文件数与体积；探测不到时只剩文件数。 */
function specLine(detail: LibraryItemDetail | null): string {
  if (!detail) return "";
  const tags = new Set<string>();
  for (const file of detail.files) {
    if (file.resolution) tags.add(file.resolution);
    if (file.hdr) tags.add(file.hdr);
  }
  const parts = [...tags];
  if (detail.file_count > 0) parts.push(`${detail.file_count} 个文件`);
  if (detail.total_size_bytes > 0) parts.push(formatBytes(detail.total_size_bytes));
  return parts.join(" · ");
}

function LibraryItemPlayCardBody({
  data,
  spec,
}: {
  data: LibraryItemCardData;
  spec: Extract<MediaCardSpec, { kind: "library_item" }>;
}) {
  const { info, detail, watch } = data;
  const pathname = usePathname();
  const meta = KIND_META[info.kind];
  const unit =
    info.kind === "tv" && spec.season !== undefined && spec.episode !== undefined
      ? { season: spec.season, episode: spec.episode }
      : null;
  const itemHref = (
    unit
      ? `/library/${info.library_id}/item/${info.media_item_id}?season=${unit.season}&episode=${unit.episode}`
      : `/library/${info.library_id}/item/${info.media_item_id}`
  ) as Route;
  const play = playHref(info.media_item_id, unit ?? undefined) as Route;
  // 退出播放回到本会话页：与最近观看卡片同一约定（走 sessionStorage，不进地址）
  const tapGuard = useTapGuard();
  const playTapGuard = useTapGuard(() => rememberPlayerReturnPath(pathname));
  const backdrop = detail?.backdrop_url ?? null;
  const poster = detail?.poster_url ?? info.poster_url;
  const context = [info.year ? String(info.year) : null, meta.label, unit ? episodeCode(unit.season, unit.episode) : null]
    .filter(Boolean)
    .join(" · ");
  const specs = specLine(detail);
  const progress =
    watch && watch.position_ms > 0 && watch.duration_ms
      ? Math.min(100, Math.round((watch.position_ms / watch.duration_ms) * 100))
      : null;
  const playVerb = watch?.played ? "重新播放" : watch && watch.position_ms > 0 ? "继续播放" : "播放";

  return (
    // 两个入口叠在一起：整卡进条目页、中央播放键直接起播。播放键是卡片链接的
    // 兄弟节点（<a> 里不能再嵌 <a>），与最近观看卡片同一结构
    <div className="group/play relative w-[248px] shrink-0 max-md:w-[212px]">
      <Link
        href={itemHref}
        scroll={false}
        aria-label={`${info.title}${context ? `，${context}` : ""}`}
        {...tapGuard}
        className="group/card block outline-none"
      >
        <div className="relative aspect-video overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.38)] ring-1 ring-white/[0.08] transition duration-300 group-hover/play:-translate-y-1 group-hover/play:shadow-[0_18px_42px_rgba(0,0,0,0.55)] group-hover/play:ring-white/25 group-focus-visible/card:ring-2 group-focus-visible/card:ring-white/80">
          {backdrop ? (
            <PosterImage
              src={imageUrl(backdrop, "landscape-card")}
              alt={`${info.title} 剧照`}
              className="size-full transition duration-500 group-hover/play:scale-[1.03]"
              fallback={<PosterFill title={info.title} posterUrl={poster} />}
            />
          ) : (
            <PosterFill title={info.title} posterUrl={poster} />
          )}
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-black/75 to-transparent" />
          {watch?.played && (
            <span className="absolute right-2 top-2 rounded-full bg-[var(--ok)] px-2 py-0.5 text-micro font-semibold text-[#07120c]">
              已看完
            </span>
          )}
          {progress !== null && (
            <div className="pointer-events-none absolute inset-x-2 bottom-2 h-[3px] overflow-hidden rounded-full bg-white/25">
              <div className="h-full rounded-full bg-[var(--accent-2)]" style={{ width: `${progress}%` }} />
            </div>
          )}
        </div>
        <p className="mt-2 truncate text-ui font-semibold text-[var(--text)]">{info.title}</p>
        {context && (
          <p className="tnum mt-0.5 truncate text-sub text-[var(--text-muted)]">{context}</p>
        )}
        {specs && (
          <p className="tnum mt-0.5 truncate text-caption text-[var(--text-faint)]">{specs}</p>
        )}
      </Link>
      <div className="pointer-events-none absolute inset-x-0 top-0 flex aspect-video items-center justify-center transition duration-300 group-hover/play:-translate-y-1">
        <Link
          href={play}
          aria-label={`${playVerb}《${info.title}》${unit ? ` ${episodeCode(unit.season, unit.episode)}` : ""}`}
          {...playTapGuard}
          className="pointer-events-auto flex size-11 items-center justify-center rounded-full border-[1.5px] border-white/75 text-white shadow-[0_1px_10px_rgba(0,0,0,0.45)] transition duration-200 hover:scale-[1.06] hover:border-white hover:bg-white/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 group-hover/play:border-white max-md:size-10"
        >
          <PlayIcon className="size-9 drop-shadow-[0_1px_2px_rgba(0,0,0,0.55)] max-md:size-8" />
        </Link>
      </div>
    </div>
  );
}

/** 没有横向剧照时：海报模糊铺底，中央完整保留一张清晰海报。 */
function PosterFill({ title, posterUrl }: { title: string; posterUrl: string | null }) {
  if (!posterUrl) {
    return (
      <span className="flex size-full items-center justify-center px-5 text-center text-ui font-semibold text-white/25">
        {title}
      </span>
    );
  }
  const src = imageUrl(posterUrl, "poster-card");
  return (
    <div className="relative size-full overflow-hidden bg-[#10131c]">
      <PosterImage src={src} alt="" className="absolute inset-0 size-full scale-125 opacity-45 blur-xl" />
      <PosterImage
        src={src}
        alt={`${title} 海报`}
        className="absolute inset-y-0 left-1/2 aspect-[2/3] h-full -translate-x-1/2 object-cover"
      />
    </div>
  );
}

/* —— 卡片组与时间线接入 —— */

function MediaCard({ spec }: { spec: MediaCardSpec }) {
  if (spec.kind === "library") return <LibraryMiniCard libraryId={spec.libraryId} />;
  if (spec.kind === "title") return <TitlePosterCard spec={spec} />;
  return <LibraryItemPlayCard spec={spec} />;
}

/** 一次 render_media_cards 调用绘制的一组卡片：可选小标题 + 横滚行。 */
export const AgentMediaCardsBlock = memo(function AgentMediaCardsBlock({
  group,
}: {
  group: MediaCardGroup;
}) {
  return (
    <section className="min-w-0">
      {group.title && (
        <h4 className="mb-2 text-sub font-semibold text-[var(--text-muted)]">{group.title}</h4>
      )}
      {/* 负外边距让首张卡与正文左缘对齐，横滚时卡片仍能贴着容器边滑出 */}
      <HScroller className="-mx-1 gap-3 px-1 pb-2 pt-1">
        {group.cards.map((spec) => (
          <MediaCard key={spec.key} spec={spec} />
        ))}
      </HScroller>
    </section>
  );
});

/** 一次工具调用是否已经失败：流式期间由回执标记，回放数据靠 runner 的失败前缀识别。 */
function toolFailed(tool: Extract<AgentProcessItem, { kind: "tool" }>): boolean {
  return Boolean(tool.isError) || Boolean(tool.output?.startsWith("工具执行失败："));
}

/**
 * 处理过程块里所有 render_media_cards 调用的卡片组，按调用顺序排在该块之后。
 * 参数尚未生成完（argsDone=false）或调用已失败（后端校验拒绝）的不画：
 * 前者还没有可画的东西，后者模型会按错误回执重发一次正确的调用。
 */
export const AgentMediaCardsForSegment = memo(function AgentMediaCardsForSegment({
  segment,
}: {
  segment: AgentTurnSegment & { kind: "process" };
}) {
  const groups = useMemo(
    () =>
      segment.items.flatMap((item) => {
        if (item.kind !== "tool" || item.argsDone === false || toolFailed(item)) return [];
        const group = parseMediaCardsArgs(item.name, item.args);
        return group ? [{ id: item.id, group }] : [];
      }),
    [segment.items],
  );
  if (groups.length === 0) return null;
  return (
    <>
      {groups.map(({ id, group }) => (
        <AgentMediaCardsBlock key={id} group={group} />
      ))}
    </>
  );
});
