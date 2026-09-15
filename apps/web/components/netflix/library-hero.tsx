"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { InfoIcon, PlayIcon, SparkIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { type LibraryItem, listLibraries, listLibraryItems } from "@/lib/api/libraries";
import { listUpNext, type UpNextItem } from "@/lib/api/playback";
import { imageUrl, upgradedTmdbOriginalUrl, cardVariantFor } from "@/lib/image-proxy";
import { formatRelativeTime } from "@/lib/time";

/**
 * Netflix 主题媒体库页（/library）顶部的全出血 Billboard——即原「内容首页」
 * 的首屏（2026-09 修订：首页概念退役、与媒体库合并成页，/ 在该主题下 replace
 * 到 /library，见 docs/design/web-themes.md §5.3）。
 *
 * 选片规则不变（确定性两档）：「接下来继续」第一项（/playback/up-next）→
 * 否则「最近入库」第一项（跨可见库聚合）。只喂 Billboard 一张卡，每库取数
 * 上限因此降到 1；原首页的「继续观看 / 最近入库 / 订阅 / 各库」内容行随合并
 * 退役——媒体库页的行清单已覆盖同样的来源（且可按人自定义）。两档全空
 * （全新部署）时渲染一小段让位空档，由媒体库页自己的空态卡接管引导。
 *
 * hero 跟随页面滚动（挂在媒体库页的滚动容器内），从透明顶栏底下直出——
 * 这正是 Netflix 的首屏构图（外壳对 /library 不加顶栏让位，见 app-shell 的
 * isHome）。
 */

export function NetflixLibraryHero() {
  const [upNext, setUpNext] = useState<UpNextItem[] | null>(null);
  const [itemsByLibrary, setItemsByLibrary] = useState<Map<number, LibraryItem[]> | null>(null);

  useEffect(() => {
    // 与媒体库页同一套失败策略：单路失败按空处理，另一路照常选片，不拖垮 hero
    let cancelled = false;
    listUpNext(1)
      .catch(() => [])
      .then((items) => {
        if (!cancelled) setUpNext(items);
      });
    listLibraries()
      .then(async (libs) => {
        const visible = libs.filter((l) => l.viewer_access && !l.exclude_from_home);
        const entries = await Promise.all(
          visible.map(async (lib) => {
            try {
              return [lib.id, await listLibraryItems(lib.id, { sort: "added_at", limit: 1 })] as const;
            } catch {
              return [lib.id, [] as LibraryItem[]] as const;
            }
          }),
        );
        if (!cancelled) setItemsByLibrary(new Map(entries));
      })
      .catch(() => {
        if (!cancelled) setItemsByLibrary(new Map());
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // —— Billboard 选片（确定性两档） ——
  const recentSorted = useMemo(() => {
    const all: LibraryItem[] = [];
    for (const items of itemsByLibrary?.values() ?? []) all.push(...items);
    return all.sort((a, b) => (b.added_at ?? "").localeCompare(a.added_at ?? ""));
  }, [itemsByLibrary]);

  const billboard = useMemo(() => {
    if (upNext?.length) return { kind: "upnext" as const, item: upNext[0] };
    if (recentSorted.length) return { kind: "library" as const, item: recentSorted[0] };
    return null;
  }, [upNext, recentSorted]);

  const loading = upNext === null || itemsByLibrary === null;

  if (billboard) {
    return (
      <NetflixBillboard
        upNextItem={billboard.kind === "upnext" ? billboard.item : null}
        libraryItem={billboard.kind === "library" ? billboard.item : null}
      />
    );
  }
  // 加载中给与 billboard 同高的占位，数据到位不再跳动。两档全空（全新部署）
  // 也不能返回 null：外壳对 Netflix 的 /library 不加顶栏让位（isHome 氛围页），
  // 空档消失会让「媒体库」页头顶进 68px 的透明顶栏底下被字标盖住——留一段
  // 小空档把内容压到顶栏下方，引导交给媒体库页自己的空态卡。
  return loading ? (
    <div className="flex h-[40vh] min-h-[320px] items-center justify-center text-ui text-[var(--text-muted)] max-md:min-h-[300px] md:h-[clamp(480px,56.25vw,80vh)]">
      <span className="size-5 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
    </div>
  ) : (
    <div aria-hidden="true" className="h-16 max-md:h-6" />
  );
}

/* ================================ Billboard ================================ */

/**
 * 全出血 hero：高度按 16:9 推导并以视口收口 `clamp(480px, 56.25vw, 80vh)`
 * （移动 40vh，自家决策值）；画面层（.nf-billboard-art）与详情页沉浸剧照
 * 同一套语言（globals.css）：左侧多段缓变黑遮罩、底部渐隐入画布纯黑、下滚
 * 渐暗 + 模糊退场。左下文案块（标题 / 元数据 / 按钮）与「▶ 播放（白底黑字）
 * · ⓘ 详情（灰底）· ✦ 问 AI」。
 */
function NetflixBillboard({
  upNextItem,
  libraryItem,
}: {
  upNextItem: UpNextItem | null;
  libraryItem: LibraryItem | null;
}) {
  const router = useRouter();
  const title = upNextItem?.title ?? libraryItem?.title ?? "";
  const meta = upNextItem
    ? [
        upNextItem.year && upNextItem.year > 0 ? String(upNextItem.year) : null,
        upNextItem.kind === "tv" ? "剧集" : "电影",
        upNextContext(upNextItem),
      ]
    : [
        libraryItem && libraryItem.year ? String(libraryItem.year) : null,
        libraryItem ? kindLabel(libraryItem.kind) : null,
      ]
  const metaText = meta.filter(Boolean).join(" · ");
  // 画面：横版剧照直出（billboard 的正确素材）——继续观看用集剧照/背景图，
  // 库内条目用接口给的 backdrop_url（与海报同一套本地资产优先规则）。
  // 连横图都没有的条目（家庭录像等）才回落海报模糊铺底。
  const artworkUrl = upNextItem
    ? (upNextItem.episode_still_url ?? upNextItem.backdrop_url)
    : (libraryItem?.backdrop_url ?? null);
  const playHref = upNextItem
    ? playHrefOf(upNextItem)
    : libraryItem && libraryItem.media_item_id != null
      ? (`/play/${libraryItem.media_item_id}` as Route)
      : null;
  const detailHref = upNextItem ? itemHrefOf(upNextItem) : libraryItem && libraryItem.library_id != null
    ? (`/library/${libraryItem.library_id}/item/${libraryItem.media_item_id}` as Route)
    : null;
  const progress = upNextItem?.progress_percent ?? null;
  // 入库时间做副文案（最近入库兜底时回答「为什么它在这儿」）
  const addedLabel =
    !upNextItem && libraryItem?.added_at ? `${formatRelativeTime(libraryItem.added_at)}入库` : null;

  // 沉浸画面只走高清（与发现详情页同一条「宁黑勿糊」决策）：TMDB 图升 original
  // 尺寸档，本地资产直取原图；加载并解码完成才显示，不存在「先低清后高清」的
  // 换图过程。landscape-card（480×270）只作兜底：无更高清档或高清加载失败时用。
  const fallbackSrc = artworkUrl ? imageUrl(artworkUrl, "landscape-card") : "";
  const hdUrl = artworkUrl ? upgradedTmdbOriginalUrl(imageUrl(artworkUrl)) : "";
  const [artState, setArtState] = useState<"pending" | "ok" | "failed">("pending");
  useEffect(() => {
    if (!hdUrl) {
      setArtState("failed"); // 没有画面素材：走海报模糊铺底兜底
      return;
    }
    setArtState("pending");
    let cancelled = false;
    const img = new Image();
    const settle = () => {
      img
        .decode()
        .catch(() => {})
        .then(() => {
          if (!cancelled) setArtState("ok");
        });
    };
    img.onload = settle;
    img.onerror = () => {
      if (!cancelled) setArtState("failed");
    };
    img.src = hdUrl;
    return () => {
      cancelled = true;
    };
  }, [hdUrl, fallbackSrc]);
  const artSrc = artState === "ok" ? hdUrl : artState === "failed" ? fallbackSrc : "";

  // 滚动退场（详情页 --nf-hero-recede 的 billboard 版）：billboard 跟随媒体库
  // 页的滚动容器滚走，下滚时画面渐暗 + 模糊。进度写在 section 元素上，只有
  // 本组件的子树消费它（.nf-billboard-art，见 globals.css），不挂
  // html.nf-hero-live——那会牵连全站沉浸覆盖层的规则。
  const sectionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    // 找到所在的滚动容器（全站页面都是「外壳固定 + 内层 overflow-y-auto」，
    // 与 PageNav 同款行走法）；挂载时先读真实 scrollTop——媒体库页带着恢复的
    // 滚动位置回来时没有滚动事件可听，不能默认 0
    let found: HTMLElement | null = section.parentElement;
    while (
      found &&
      getComputedStyle(found).overflowY !== "auto" &&
      getComputedStyle(found).overflowY !== "scroll"
    ) {
      found = found.parentElement;
    }
    if (!found) return;
    const scroller = found;
    let frame = 0;
    const sync = () => {
      frame = 0;
      // 归一化尺度取画面高度的 80%：billboard 比详情页 hero 更早滚出视口，
      // 缓出区间相应收短
      const range = Math.max(240, section.clientHeight * 0.8);
      const progress = Math.min(1, Math.max(0, scroller.scrollTop / range));
      section.style.setProperty("--nf-hero-recede", progress.toFixed(3));
    };
    const onScroll = () => {
      if (!frame) frame = window.requestAnimationFrame(sync);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    sync();
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <section
      ref={sectionRef}
      aria-label={`正在展示《${title}》`}
      className="relative h-[40vh] min-h-[320px] w-full max-md:min-h-[300px] md:h-[clamp(480px,56.25vw,80vh)]"
    >
      {/* 画面：高清剧照直出（加载解码完成前保持黑场）；无剧照的条目用海报
          模糊铺底兜底。遮罩语言（左黑渐变 / 底部渐隐 / 滚动模糊）全部收口在
          .nf-billboard-art（globals.css，与详情页沉浸剧照同一套规则） */}
      <div className="absolute inset-0 overflow-hidden bg-black">
        {artSrc ? (
          <div className="nf-billboard-art absolute inset-0">
            <img src={artSrc} alt="" className="size-full object-cover" />
          </div>
        ) : artworkUrl ? null : libraryItem?.poster_url ? (
          <PosterFallbackFill url={libraryItem.poster_url} aspect={libraryItem.primary_aspect} />
        ) : null}
      </div>

      {/* 左下文案块 */}
      <div className="absolute inset-x-0 bottom-[12%] px-[4vw] max-md:bottom-[10%]">
        <h1 className="max-w-[80%] text-[clamp(28px,4.6vw,56px)] font-bold leading-[1.12] tracking-[-0.02em] text-white text-on-image max-md:max-w-full">
          {title}
        </h1>
        {metaText && (
          <p className="tnum text-on-image mt-2.5 flex flex-wrap items-center gap-x-2.5 text-[14px] font-medium text-[#e5e5e5] max-md:mt-2">
            <span className="font-bold text-[var(--ok)]">在库</span>
            {metaText}
          </p>
        )}
        {addedLabel && (
          <p className="text-on-image mt-1 text-caption text-[var(--text-muted)]">{addedLabel}</p>
        )}
        {/* 按钮组：▶ 播放（白底黑字）· ⓘ 详情（灰底）· ✦ 问 AI（ghost）。
            移动端可换行（320px 视口三颗排不下）且保持 44px 触控高度——
            billboard 按钮组是最高频的操作区，max-md:h-9 的 36px 偏小 */}
        <div className="mt-4 flex items-center gap-2.5 max-md:mt-3.5 max-md:flex-wrap">
          {playHref && (
            <button
              type="button"
              onClick={() => router.push(playHref)}
              className="flex h-10 items-center gap-2 rounded-[4px] bg-white px-5 text-[15px] font-bold text-black transition-colors hover:bg-white/75 max-md:h-11 max-md:px-4"
            >
              <PlayIcon className="size-5" fill="currentColor" />
              播放
            </button>
          )}
          {detailHref && (
            <button
              type="button"
              onClick={() => router.push(detailHref)}
              className="flex h-10 items-center gap-2 rounded-[4px] bg-[rgba(109,109,110,0.7)] px-5 text-[15px] font-semibold text-white transition-colors hover:bg-[rgba(109,109,110,0.4)] max-md:h-11 max-md:px-4"
            >
              <InfoIcon className="size-5" />
              更多信息
            </button>
          )}
          {/* AI 是本站差异能力：Netflix 没有但至少不破坏画面的第三个入口 */}
          <button
            type="button"
            onClick={() => router.push("/new")}
            className="text-on-image flex h-10 items-center gap-1.5 rounded-[4px] px-3 text-[15px] font-medium text-[var(--text-muted)] transition-colors hover:text-white max-md:h-11"
          >
            <SparkIcon className="size-4" />
            问 AI
          </button>
        </div>
        {/* 继续观看的进度：billboard 底部细红条（与播放器进度同语言） */}
        {progress != null && progress > 0 && (
          <div className="mt-4 h-[3px] w-[min(320px,60%)] bg-white/25 max-md:mt-3">
            <div className="h-full bg-[var(--accent)]" style={{ width: `${progress}%` }} />
          </div>
        )}
      </div>
    </section>
  );
}

/** 无横版剧照时的 billboard 兜底：海报放大模糊铺底 + 中央完整显示 */
function PosterFallbackFill({ url, aspect }: { url: string; aspect: number }) {
  const src = imageUrl(url, cardVariantFor(aspect));
  return (
    <>
      <img src={src} alt="" className="absolute inset-0 size-full scale-110 object-cover opacity-40 blur-2xl" />
      <div className="absolute inset-0 flex items-center justify-center">
        <div
          style={{ aspectRatio: aspect, height: "86%" }}
          className="overflow-hidden rounded-[4px] shadow-[0_0_40px_rgba(0,0,0,0.6)]"
        >
          <PosterImage src={src} alt="" className="size-full" />
        </div>
      </div>
    </>
  );
}

/* ============================== 数据适配层 ============================== */

/**
 * 继续观看的副文案：剧集给「S01E02 · 集名」；电影没有集数上下文、
 * 年份已由调用方的 meta 行给出，返回空串（否则年份会显示两遍）。
 */
function upNextContext(item: UpNextItem): string {
  if (item.kind !== "tv") return "";
  const code = `S${String(item.season_number).padStart(2, "0")}E${String(item.episode_number).padStart(2, "0")}`;
  return [code, item.episode_title].filter(Boolean).join(" · ");
}

/** 直接播放：续播点由服务端在开会话时解析（§6.10），前端不重复计算。 */
function playHrefOf(item: UpNextItem): Route {
  return (
    item.kind === "tv"
      ? `/play/${item.media_item_id}/s${String(item.season_number).padStart(2, "0")}e${String(item.episode_number).padStart(2, "0")}`
      : `/play/${item.media_item_id}`
  ) as Route;
}

function itemHrefOf(item: UpNextItem): Route {
  const base = `/library/${item.library_id}/item/${item.media_item_id}`;
  return (
    item.kind === "tv"
      ? `${base}?season=${item.season_number}&episode=${item.episode_number}&from=recent`
      : `${base}?from=recent`
  ) as Route;
}

function kindLabel(kind: LibraryItem["kind"]): string {
  return kind === "tv" ? "剧集" : kind === "movie" ? "电影" : "视频";
}
