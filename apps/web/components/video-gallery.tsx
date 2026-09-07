"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { OpenIcon, PlayIcon } from "@/components/icons";
import {
  DENSITY,
  layoutMasonry,
  layoutSparseRow,
  type DensitySpec,
  type PhotoWallDensity,
} from "@/components/photo-wall";
import { PosterImage } from "@/components/poster-image";
import { ZoomLightbox, type ZoomLightboxSlide } from "@/components/zoom-lightbox";
import type { LibraryGalleryGroup, LibraryGalleryImage } from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";
import { playHref, rememberPlayerReturnPath } from "@/lib/player/play-links";

/**
 * 影视库 / 其他库的「图床浏览模式」：把每部作品的海报、剧照、分集剧照与
 * 章节场景图铺成一面瀑布流墙，样式与交互照搬图片库的相册墙与灯箱
 * （photo-wall / zoom-lightbox），只有两处不同：
 *   - 分组按**作品**而不是按月：一部作品一段标题 + 一面墙，标题可点进详情。
 *     分组可以在 ⋯ 菜单里关掉——关掉之后整库的图混成一条瀑布流，谁也不分段，
 *     纯看图时最沉浸（用户决策 2026-09-07）；
 *   - 灯箱顶栏右侧不是「下载 / 拍摄信息」，而是「播放 / 详情」：章节场景图的
 *     播放就是从那一帧起播（服务端给了 t_seconds），分集剧照带季集号进详情页
 *     直接落到那一集。
 *
 * 数据来自 /libraries/{id}/gallery，按作品分页（与海报墙同一份标题序）；
 * 灯箱翻的是铺平后的整份图列表（GalleryEntry），翻到末尾向外要下一页。
 */

const MODE_STORAGE_KEY = "movieclaw.library.gallery-mode";
const GROUPED_STORAGE_KEY = "movieclaw.library.gallery-grouped";

/**
 * 记在浏览器里的开关偏好：只是浏览便利设置（不是账号数据），读不到就用默认值。
 * 首帧一律先给默认值、挂载后再读 storage——服务端渲染没有 localStorage，
 * 直接在 useState 初始值里读会导致首屏与水合后不一致。
 */
function useStoredFlag(key: string, fallback: boolean): [boolean, (next: boolean) => void] {
  const [value, setValue] = useState(fallback);
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored !== null) setValue(stored === "1");
    } catch {
      /* 隐私模式等拿不到 storage：保持默认值 */
    }
  }, [key]);
  const update = useCallback(
    (next: boolean) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, next ? "1" : "0");
      } catch {
        /* 同上 */
      }
    },
    [key],
  );
  return [value, update];
}

/** 读写「图床浏览模式」偏好：进详情再退回来还在图廊 */
export function useVideoGalleryMode(): [boolean, (next: boolean) => void] {
  return useStoredFlag(MODE_STORAGE_KEY, false);
}

/** 读写「按作品分组」偏好：默认分组，关掉就是整库一条瀑布流 */
export function useVideoGalleryGrouped(): [boolean, (next: boolean) => void] {
  return useStoredFlag(GROUPED_STORAGE_KEY, true);
}

/** 铺平后的一张图：知道自己属于哪部作品，播放 / 详情按钮据此拼地址 */
export interface GalleryEntry {
  group: LibraryGalleryGroup;
  image: LibraryGalleryImage;
}

export function flattenGallery(groups: readonly LibraryGalleryGroup[]): GalleryEntry[] {
  return groups.flatMap((group) => group.images.map((image) => ({ group, image })));
}

function groupTitle(group: LibraryGalleryGroup): string {
  return group.year ? `${group.title} (${group.year})` : group.title;
}

/** 条目详情页地址：分集剧照 / 剧集章节图带季集号，详情页直接落到那一集 */
function detailHref(libraryId: number, entry: GalleryEntry): Route {
  const { group, image } = entry;
  const base = `/library/${libraryId}/item/${group.media_item_id}`;
  const unit =
    image.season !== null && image.episode !== null
      ? `?season=${image.season}&episode=${image.episode}`
      : "";
  return `${base}${unit}` as Route;
}

export function VideoGalleryWall({
  groups,
  density,
  grouped,
  onOpen,
  libraryId,
}: {
  /** 已加载的作品分组（服务端标题序） */
  groups: LibraryGalleryGroup[];
  density: PhotoWallDensity;
  /** 按作品分段；false = 整库的图混成一条瀑布流，没有段标题 */
  grouped: boolean;
  /** 点击某张：传的是它在铺平列表（flattenGallery）里的下标，灯箱按同一列表翻页 */
  onOpen: (index: number) => void;
  libraryId: number;
}) {
  // 铺平的整份列表：不分组时直接排它；分组时用来算每段的起始下标
  // （灯箱按整份列表翻页，瓦片点击要给全局下标）
  const entries = useMemo(() => flattenGallery(groups), [groups]);
  const starts = useMemo(() => {
    let acc = 0;
    return groups.map((group) => {
      const start = acc;
      acc += group.images.length;
      return start;
    });
  }, [groups]);

  // 容器宽度：ResizeObserver 驱动重排；首帧用 layout effect 量一次，避免闪一下空墙
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? el.clientWidth);
      setWidth((current) => (current === next ? current : next));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const spec = DENSITY[density];

  return (
    <div ref={containerRef} className="min-w-0">
      {width > 0 &&
        (grouped ? (
          groups.map((group, i) => (
            <GalleryGroupSection
              key={group.media_item_id}
              group={group}
              start={starts[i]}
              width={width}
              spec={spec}
              onOpen={onOpen}
              libraryId={libraryId}
            />
          ))
        ) : (
          <GalleryTiles entries={entries} start={0} width={width} spec={spec} onOpen={onOpen} />
        ))}
    </div>
  );
}

/**
 * 一面瀑布流：分组模式下是一部作品的图，不分组时是整库的图。
 *
 * ``start`` 是本面墙第一张在铺平列表里的下标——瓦片点击要给灯箱全局下标。
 */
const GalleryTiles = memo(function GalleryTiles({
  entries,
  start,
  width,
  spec,
  onOpen,
}: {
  entries: GalleryEntry[];
  start: number;
  width: number;
  spec: DensitySpec;
  onOpen: (index: number) => void;
}) {
  const layout = useMemo(() => {
    const aspects = entries.map((entry) => entry.image.aspect);
    const masonry = layoutMasonry(aspects, width, spec.column, spec.gap, spec.minColumns);
    // 图少于列数：瀑布流会退化成孤柱，改一行等高（同相册墙的稀疏月份）
    return aspects.length < masonry.columns
      ? layoutSparseRow(aspects, width, spec.column, spec.gap)
      : masonry;
  }, [entries, width, spec]);
  return (
    <div className="relative" style={{ height: layout.height }}>
      {entries.map(({ group, image }, i) => (
        // key 带上条目 id：不分组时整库的图排在一面墙上，光靠 url 不保证唯一
        <GalleryTile
          key={`${group.media_item_id}:${image.kind}:${image.url}`}
          group={group}
          image={image}
          placement={layout.placements[i]}
          spec={spec}
          onOpen={() => onOpen(start + i)}
        />
      ))}
    </div>
  );
});

const GalleryGroupSection = memo(function GalleryGroupSection({
  group,
  start,
  width,
  spec,
  onOpen,
  libraryId,
}: {
  group: LibraryGalleryGroup;
  /** 本组第一张在铺平列表里的下标 */
  start: number;
  width: number;
  spec: DensitySpec;
  onOpen: (index: number) => void;
  libraryId: number;
}) {
  const entries = useMemo(
    () => group.images.map((image) => ({ group, image })),
    [group],
  );
  return (
    <section className="mb-8 last:mb-0">
      <div className="mb-3 flex items-baseline gap-2.5">
        <Link
          href={`/library/${libraryId}/item/${group.media_item_id}` as Route}
          className="text-on-image truncate text-body-lg font-semibold text-white/85 transition-colors hover:text-white"
        >
          {groupTitle(group)}
        </Link>
        <span className="tnum shrink-0 text-caption text-[var(--text-faint)]">
          {group.images.length} 张
        </span>
      </div>
      <GalleryTiles entries={entries} start={start} width={width} spec={spec} onOpen={onOpen} />
    </section>
  );
});

const GalleryTile = memo(function GalleryTile({
  group,
  image,
  placement,
  spec,
  onOpen,
}: {
  group: LibraryGalleryGroup;
  image: LibraryGalleryImage;
  placement: { x: number; y: number; width: number; height: number };
  spec: DensitySpec;
  onOpen: () => void;
}) {
  return (
    // 与相册墙的瓦片同一套：绝对定位 + transform 重排走过渡，content-visibility
    // 让视口外的瓦片跳过绘制
    <button
      type="button"
      aria-label={`查看 ${group.title} · ${image.label}`}
      onClick={onOpen}
      className="group/tile absolute left-0 top-0 block overflow-hidden rounded-xl bg-[#141824] text-left shadow-[0_8px_22px_rgba(0,0,0,0.35)] ring-1 ring-white/[0.07] transition-[transform,width,height,box-shadow] duration-300 ease-out [content-visibility:auto] hover:z-[2] hover:shadow-[0_18px_44px_rgba(0,0,0,0.6)] hover:ring-white/25 focus-visible:z-[2] focus-visible:ring-2 focus-visible:ring-[var(--accent)] motion-reduce:transition-none"
      style={{
        transform: `translate(${Math.round(placement.x)}px, ${Math.round(placement.y)}px)`,
        width: Math.round(placement.width),
        height: Math.round(placement.height),
      }}
    >
      <PosterImage
        src={imageUrl(image.url, spec.variant)}
        alt={`${group.title} · ${image.label}`}
        className="absolute inset-0 size-full object-cover transition-transform duration-500 ease-out group-hover/tile:scale-[1.04] motion-reduce:transition-none"
      />
      {/* 不给常驻角标（用户决策 2026-09-07）：几百个「海报 / 剧照 / 第 3 集」浮在
          墙上，视线全被标签牵走，瀑布流就不是一面图墙了。是哪一类图移到下面的
          悬停层里说——真要分辨时鼠标一停就有，平时画面干净。
          悬停信息层：作品名 + 图的说明。不用 backdrop-blur（几百张瓦片叠加会拖慢滚动） */}
      <span className="pointer-events-none absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-[rgba(6,8,14,0.85)] via-[rgba(6,8,14,0.25)] to-transparent p-2.5 opacity-0 transition-opacity duration-200 group-hover/tile:opacity-100 group-focus-visible/tile:opacity-100">
        <span className="truncate text-caption font-semibold text-white">{groupTitle(group)}</span>
        <span className="truncate text-micro text-white/70">
          {image.t_seconds !== null ? `从 ${formatSeconds(image.t_seconds)} 起播` : image.label}
        </span>
      </span>
    </button>
  );
});

function formatSeconds(total: number): string {
  const whole = Math.floor(total);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

export function VideoGalleryLightbox({
  libraryId,
  entries,
  index,
  hasMore,
  onIndexChange,
  onReachEnd,
  onClose,
}: {
  libraryId: number;
  /** 铺平后的整份已加载图列表（与墙同一顺序） */
  entries: GalleryEntry[];
  index: number;
  hasMore: boolean;
  onIndexChange: (index: number) => void;
  onReachEnd: () => void;
  onClose: () => void;
}) {
  const router = useRouter();
  const entry = entries[index];
  const slides = useMemo<ZoomLightboxSlide[]>(
    () =>
      entries.map(({ group, image }, i) => ({
        key: i,
        title: `${groupTitle(group)} · ${image.label}`,
        // 墙上的派生图先铺底，主图直接是服务端给的这张（海报 w780 / 剧照 w1280 /
        // 本地资产原件），没有再高一级的原图
        thumbUrl: imageUrl(image.url, "photo-tile"),
        screenUrl: imageUrl(image.url),
        aspect: image.aspect,
      })),
    [entries],
  );

  /** 播放：章节图从那一帧起播，分集剧照播那一集，海报 / 剧照从头（或续播） */
  const play = useCallback(() => {
    if (!entry) return;
    const { group, image } = entry;
    // 退出播放要回到这个库页（与详情页的播放键同一约定，见 play-links）
    rememberPlayerReturnPath(window.location.pathname + window.location.search);
    router.push(
      playHref(group.media_item_id, {
        season: image.season ?? undefined,
        episode: image.episode ?? undefined,
        tSeconds: image.t_seconds ?? undefined,
      }) as Route,
    );
  }, [entry, router]);

  if (!entry) return null;
  const playLabel = entry.image.t_seconds !== null ? "从此处播放" : "播放";
  const buttonClass =
    "rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white";

  return (
    <ZoomLightbox
      label={`查看图片：${entry.group.title} · ${entry.image.label}`}
      slides={slides}
      index={index}
      hasMore={hasMore}
      onIndexChange={onIndexChange}
      onReachEnd={onReachEnd}
      onClose={onClose}
      actions={
        <>
          <button
            type="button"
            title={playLabel}
            aria-label={playLabel}
            onClick={play}
            className={buttonClass}
          >
            <PlayIcon className="size-[18px]" />
          </button>
          <Link
            href={detailHref(libraryId, entry)}
            title="前往影片详情"
            aria-label="前往影片详情"
            className={buttonClass}
          >
            <OpenIcon className="size-[18px]" />
          </Link>
        </>
      }
    />
  );
}
