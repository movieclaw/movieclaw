"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { CheckIcon, HeartIcon, OpenIcon, PlayIcon } from "@/components/icons";
import {
  DENSITY,
  layoutMasonry,
  layoutSparseRow,
  type DensitySpec,
  type PhotoWallDensity,
} from "@/components/photo-wall";
import { PosterImage } from "@/components/poster-image";
import {
  LIGHTBOX_ACTION_CLASS,
  LIGHTBOX_ACTION_ICON_CLASS,
  ZoomLightbox,
  type ZoomLightboxSlide,
} from "@/components/zoom-lightbox";
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
 *   - 灯箱顶栏右侧不是「下载 / 拍摄信息」，而是「播放 / 收藏 / 详情」：章节场景图的
 *     播放就是从那一帧起播（服务端给了 t_seconds），分集剧照带季集号进详情页
 *     直接落到那一集；心与详情页那颗是同一颗（收藏整部作品，见 playback marks）。
 *
 * 数据来自 /libraries/{id}/gallery，按作品分页（与海报墙同一份标题序）；
 * 灯箱翻的是铺平后的整份图列表（GalleryEntry），翻到末尾向外要下一页。
 *
 * 「全部收藏」页复用同一套墙与灯箱，数据换成 /playback/favorites/gallery
 * （跨库、最近收藏在前）。因此详情落点库不是整墙一个，而是每组自带
 * （LibraryGalleryGroup.library_id）。
 */

/** 图床浏览模式一页的作品数：一部作品十来张图，24 部约一屏半 */
export const GALLERY_PAGE_SIZE = 24;
/**
 * 图床浏览模式提前取下一页的距离：约一屏半，也就是当前这一页快滑完时就去要
 * 下一页。图大、下载慢，等滑到底再发请求接上来的就是一屏空瓦片。
 */
export const GALLERY_LOAD_MARGIN = "1200px 0px";

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

/**
 * 看图的两个偏好（按作品分组 / 瀑布流密度），作为菜单项交给调用方的 ⋯ 菜单。
 *
 * 三处菜单要用到它：单库页的图床浏览（两项都有）、图片库的相册墙（只有密度，
 * 它没有分组一说）、「全部收藏」页的图廊（两项都有，且整个菜单只有这一组）。
 * 每一半都可以不给——不给就不渲染那一半。菜单外壳（触发键、Content）留在各自
 * 的调用方：全站惯例是每个菜单自带外壳与行样式（itemClass 因此由调用方传入），
 * 这里只共享真正相同的那部分——项目本身。
 */
export function WallPrefItems({
  grouped,
  onGroupedChange,
  density,
  onDensityChange,
  itemClass,
}: {
  grouped?: boolean;
  onGroupedChange?: (next: boolean) => void;
  density?: PhotoWallDensity;
  onDensityChange?: (next: PhotoWallDensity) => void;
  /** 调用方菜单的行样式：同一个菜单里各项长相必须一致 */
  itemClass: string;
}) {
  return (
    <>
      {/* 图床浏览：关掉分组，整墙的图就混成一条瀑布流 */}
      {grouped !== undefined && onGroupedChange && (
        <DropdownMenu.CheckboxItem
          checked={grouped}
          onCheckedChange={onGroupedChange}
          className={`${itemClass} flex items-center justify-between`}
        >
          按作品分组
          <DropdownMenu.ItemIndicator>
            <CheckIcon className="size-3.5 text-[var(--accent)]" />
          </DropdownMenu.ItemIndicator>
        </DropdownMenu.CheckboxItem>
      )}
      {density && onDensityChange && (
        <>
          <DropdownMenu.Label className="px-3 pb-1 pt-1.5 text-caption text-[var(--text-faint)]">
            瀑布流密度
          </DropdownMenu.Label>
          <DropdownMenu.RadioGroup
            value={density}
            onValueChange={(next) => onDensityChange(next as PhotoWallDensity)}
          >
            {(
              [
                ["compact", "紧凑"],
                ["standard", "标准"],
                ["loose", "宽松"],
              ] as [PhotoWallDensity, string][]
            ).map(([key, label]) => (
              <DropdownMenu.RadioItem
                key={key}
                value={key}
                className={`${itemClass} flex items-center justify-between`}
              >
                {label}
                <DropdownMenu.ItemIndicator>
                  <CheckIcon className="size-3.5 text-[var(--accent)]" />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </>
      )}
    </>
  );
}

/** 铺平后的一张图：知道自己属于哪部作品，播放 / 详情按钮据此拼地址 */
export interface GalleryEntry {
  group: LibraryGalleryGroup;
  image: LibraryGalleryImage;
}

/**
 * 图廊分组上墙前的统一口径：没图的作品不占位（服务端按作品分页，空组只用来
 * 数页），同一部作品只留最前面那一组。追加下一页与整窗对账都过这一道。
 */
export function dedupeGalleryGroups(groups: LibraryGalleryGroup[]): LibraryGalleryGroup[] {
  const seen = new Set<number>();
  return groups.filter((group) => {
    if (group.images.length === 0 || seen.has(group.media_item_id)) return false;
    seen.add(group.media_item_id);
    return true;
  });
}

export function flattenGallery(groups: readonly LibraryGalleryGroup[]): GalleryEntry[] {
  return groups.flatMap((group) => group.images.map((image) => ({ group, image })));
}

function groupTitle(group: LibraryGalleryGroup): string {
  return group.year ? `${group.title} (${group.year})` : group.title;
}

/**
 * 一张瓦片的稳定标识：React key 与滚动恢复的锚点共用同一串。
 *
 * 不能只用条目 id——同一部作品在墙上有十几张图；也不能只用 url——不分组时
 * 整库的图排在一面墙上，不同条目的图床地址理论上可能撞。
 */
function tileKey(group: LibraryGalleryGroup, image: LibraryGalleryImage): string {
  return `${group.media_item_id}:${image.kind}:${image.url}`;
}

/**
 * 条目详情页地址：分集剧照 / 剧集章节图带季集号，详情页直接落到那一集。
 * 落点库取分组自己带的那个——收藏图廊是跨库的一面墙，同一面墙上各组不同库。
 */
function detailHref(entry: GalleryEntry): Route {
  const { group, image } = entry;
  const base = `/library/${group.library_id}/item/${group.media_item_id}`;
  const unit =
    image.season !== null && image.episode !== null
      ? `?season=${image.season}&episode=${image.episode}`
      : "";
  return `${base}${unit}` as Route;
}

/**
 * 提前取图的距离：约一屏半。
 *
 * 瓦片带 ``content-visibility:auto``，子树被跳过时 ``<img loading="lazy">`` 不做
 * 相交判定（根因见 poster-image.tsx 顶部的长注释），实际要等瓦片自己解除跳过
 * ——大约只提前半屏——才发第一个请求。图廊的图又比海报大，滑快一点就是一路
 * 黑格，图追在人后面。这里提前一屏半开始取，滑到时基本已经就位。
 */
const PREFETCH_MARGIN = "1200px 0px";

/**
 * 一面墙一个 IntersectionObserver，观察**瓦片本体**：瓦片有显式宽高、自己不被
 * 跳过，相交判定照常工作（被跳过的是它的子树）。进入提前量的瓦片切成 eager。
 *
 * 标记只加不减——图取过就不必再管，命中即 ``unobserve``；一次滑动会连着命中
 * 几十张，合并到下一帧统一提交，不一张一次 setState。分组模式下每段墙各持
 * 一个观察器：段与段互不影响，某一段有图进场时不必惊动整墙重渲染。
 */
function useTilePrefetch() {
  const [near, setNear] = useState<ReadonlySet<string>>(() => new Set());
  const observerRef = useRef<IntersectionObserver | null>(null);
  const pending = useRef<Set<string>>(new Set());
  const flush = useRef(0);

  const getObserver = useCallback(() => {
    if (observerRef.current || typeof IntersectionObserver === "undefined") {
      return observerRef.current;
    }
    observerRef.current = new IntersectionObserver(
      (records) => {
        for (const record of records) {
          if (!record.isIntersecting) continue;
          const key = (record.target as HTMLElement).dataset.galleryTileId;
          if (key) pending.current.add(key);
          observerRef.current?.unobserve(record.target);
        }
        if (pending.current.size > 0 && !flush.current) {
          flush.current = requestAnimationFrame(() => {
            flush.current = 0;
            setNear((current) => new Set([...current, ...pending.current]));
            pending.current.clear();
          });
        }
      },
      { rootMargin: PREFETCH_MARGIN },
    );
    return observerRef.current;
  }, []);

  useEffect(
    () => () => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      if (flush.current) cancelAnimationFrame(flush.current);
    },
    [],
  );

  // 瓦片的 ref：挂上即观察，卸载时（React 19 的 ref 清理）取消观察
  const observe = useCallback(
    (node: HTMLElement | null) => {
      if (!node) return;
      const observer = getObserver();
      observer?.observe(node);
      return () => observer?.unobserve(node);
    },
    [getObserver],
  );

  return { near, observe };
}

export function VideoGalleryWall({
  groups,
  density,
  grouped,
  onOpen,
}: {
  /** 已加载的作品分组（服务端给的顺序：单库按标题，收藏按最近收藏） */
  groups: LibraryGalleryGroup[];
  density: PhotoWallDensity;
  /** 按作品分段；false = 整墙的图混成一条瀑布流，没有段标题 */
  grouped: boolean;
  /** 点击某张：传的是它在铺平列表（flattenGallery）里的下标，灯箱按同一列表翻页 */
  onOpen: (index: number) => void;
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
  const { near, observe } = useTilePrefetch();
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
      {entries.map(({ group, image }, i) => {
        const placement = layout.placements[i];
        const key = tileKey(group, image);
        return (
          <GalleryTile
            key={key}
            group={group}
            image={image}
            index={start + i}
            preload={near.has(key)}
            observe={observe}
            x={placement.x}
            y={placement.y}
            width={placement.width}
            height={placement.height}
            spec={spec}
            onOpen={onOpen}
          />
        );
      })}
    </div>
  );
});

const GalleryGroupSection = memo(function GalleryGroupSection({
  group,
  start,
  width,
  spec,
  onOpen,
}: {
  group: LibraryGalleryGroup;
  /** 本组第一张在铺平列表里的下标 */
  start: number;
  width: number;
  spec: DensitySpec;
  onOpen: (index: number) => void;
}) {
  const entries = useMemo(
    () => group.images.map((image) => ({ group, image })),
    [group],
  );
  return (
    <section className="mb-8 last:mb-0">
      <div className="mb-3 flex items-baseline gap-2.5">
        <Link
          href={`/library/${group.library_id}/item/${group.media_item_id}` as Route}
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

/**
 * 一张瓦片。
 *
 * 位置拆成四个数字、点击给稳定的 ``onOpen`` + 自己的下标，都是为了让 ``memo``
 * 真的生效：不分组时整墙共用一份铺平列表，追加一页或翻一次收藏都会重算
 * layout，传对象（每次新引用）或内联箭头（每次新函数）会让几千个瓦片跟着
 * 全量重渲染。分组模式下每组各算各的，本来就不受影响。
 */
const GalleryTile = memo(function GalleryTile({
  group,
  image,
  index,
  preload,
  observe,
  x,
  y,
  width,
  height,
  spec,
  onOpen,
}: {
  group: LibraryGalleryGroup;
  image: LibraryGalleryImage;
  /** 本瓦片在铺平列表里的全局下标：灯箱按同一列表翻页 */
  index: number;
  /** 已进入提前量，图直接取（见 useTilePrefetch） */
  preload: boolean;
  /** 把瓦片交给墙上那个共享观察器 */
  observe: (node: HTMLElement | null) => (() => void) | void;
  x: number;
  y: number;
  width: number;
  height: number;
  spec: DensitySpec;
  onOpen: (index: number) => void;
}) {
  return (
    // 与相册墙的瓦片同一套：绝对定位 + transform 重排走过渡，content-visibility
    // 让视口外的瓦片跳过绘制
    <button
      type="button"
      ref={observe}
      // 滚动恢复的锚点（见 lib/use-scroll-restoration.ts）：离开这一屏时记下
      // 首个可见瓦片，返回时按它回位。纯像素位在窗口宽度变过（转屏、缩窗口）
      // 之后会错行——masonry 重排后同一个 y 已经不是同一批图了
      data-gallery-tile-id={tileKey(group, image)}
      // 所属作品：图廊按作品分页，「回到上次位置」记的是作品在整份排序里的
      // 位置（lib/library-wall-recall.ts），单张图的下标没法用来跳转
      data-gallery-item-id={group.media_item_id}
      aria-label={`查看 ${group.title} · ${image.label}${group.is_favorite ? "（已收藏）" : ""}`}
      onClick={() => onOpen(index)}
      className="group/tile absolute left-0 top-0 block overflow-hidden rounded-xl bg-[#141824] text-left shadow-[0_8px_22px_rgba(0,0,0,0.35)] ring-1 ring-white/[0.07] transition-[transform,width,height,box-shadow] duration-300 ease-out [content-visibility:auto] hover:z-[2] hover:shadow-[0_18px_44px_rgba(0,0,0,0.6)] hover:ring-white/25 focus-visible:z-[2] focus-visible:ring-2 focus-visible:ring-[var(--accent)] motion-reduce:transition-none"
      style={{
        transform: `translate(${Math.round(x)}px, ${Math.round(y)}px)`,
        width: Math.round(width),
        height: Math.round(height),
      }}
    >
      {/* 图廊的图比海报大得多（剧照 w1280、本地资产是原件），滑过去经常要等上
          一会儿；开脉冲占位，等待期看着是"在加载"而不是一块黑。
          取图一律走派生：相册墙的宽松密度（spec.variant 为 undefined）直接吃原图，
          那是因为图片库的墙图本来就是 720 缩略图——图廊没这层，得自己要一张 */}
      <PosterImage
        src={imageUrl(image.url, spec.variant ?? "gallery-tile")}
        alt={`${group.title} · ${image.label}`}
        pulseWhileLoading
        preload={preload}
        className="absolute inset-0 size-full object-cover transition-transform duration-500 ease-out group-hover/tile:scale-[1.04] motion-reduce:transition-none"
      />
      {/* 收藏是唯一的常驻角标：它是**状态**不是分类文案，不认标签也认得这颗心，
          而且只有被收藏的那几张才有，不会像「海报 / 剧照 / 第 3 集」那样满墙都是。
          aria 走瓦片自己的 label（上面带了「已收藏」），这里纯装饰 */}
      {group.is_favorite && (
        <span
          aria-hidden
          className="pointer-events-none absolute right-2 top-2 text-[var(--danger)] drop-shadow-[0_1px_3px_rgba(0,0,0,0.75)]"
        >
          <HeartIcon className="size-4" fill="currentColor" />
        </span>
      )}
      {/* 除上面那颗心外不给分类角标（用户决策 2026-09-07）：几百个「海报 / 剧照 / 第 3 集」浮在
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
  entries,
  index,
  hasMore,
  onIndexChange,
  onReachEnd,
  onToggleFavorite,
  onClose,
}: {
  /** 铺平后的整份已加载图列表（与墙同一顺序） */
  entries: GalleryEntry[];
  index: number;
  hasMore: boolean;
  onIndexChange: (index: number) => void;
  onReachEnd: () => void;
  /** 收藏 / 取消收藏这部作品：状态挂在分组上（墙上的角标读同一份），由外层落库与回滚 */
  onToggleFavorite: (mediaItemId: number, favorite: boolean) => void;
  onClose: () => void;
}) {
  const router = useRouter();
  const entry = entries[index];
  const slides = useMemo<ZoomLightboxSlide[]>(
    () =>
      entries.map(({ group, image }, i) => ({
        key: i,
        title: `${groupTitle(group)} · ${image.label}`,
        // 墙上的派生图先铺底，主图走屏幕适配派生（长边 2048，不放大）：源本身
        // 就没有比 w1280 剧照 / 本地资产更高一级的原图，这一层不为缩小尺寸，
        // 而是转成 WebP——同样的画质少三到五成字节，翻页跟手（图片库灯箱同款口径）
        thumbUrl: imageUrl(image.url, "photo-tile"),
        screenUrl: imageUrl(image.url, "photo-screen"),
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
  // 收藏的是整部作品（与详情页那颗心同一落点），不是当前这张图或这一集
  const favorite = entry.group.is_favorite;
  const favoriteLabel = favorite ? "取消收藏" : "收藏";

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
            className={LIGHTBOX_ACTION_CLASS}
          >
            <PlayIcon className={LIGHTBOX_ACTION_ICON_CLASS} />
          </button>
          <button
            type="button"
            title={`${favoriteLabel}《${entry.group.title}》`}
            aria-label={favoriteLabel}
            aria-pressed={favorite}
            onClick={() => onToggleFavorite(entry.group.media_item_id, !favorite)}
            className={LIGHTBOX_ACTION_CLASS}
          >
            <HeartIcon
              className={`${LIGHTBOX_ACTION_ICON_CLASS} ${favorite ? "text-[var(--danger)]" : ""}`}
              fill={favorite ? "currentColor" : "none"}
            />
          </button>
          <Link
            href={detailHref(entry)}
            title="前往影片详情"
            aria-label="前往影片详情"
            className={LIGHTBOX_ACTION_CLASS}
          >
            <OpenIcon className={LIGHTBOX_ACTION_ICON_CLASS} />
          </Link>
        </>
      }
    />
  );
}
