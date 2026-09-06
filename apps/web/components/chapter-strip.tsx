"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { HScroller } from "@/components/h-scroller";
import { PlayIcon } from "@/components/icons";
import { ImageLightbox } from "@/components/image-lightbox";
import { PosterImage } from "@/components/poster-image";
import type { LibraryChapter } from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";
import { formatClock } from "@/lib/player/timeline";

/**
 * 条目详情页的「场景」横排（docs/design/video-chapters.md §1.1）：每个章节一张
 * 16:9 场景图 + 时间戳角标 + 标题；点图进灯箱看大图，从灯箱或 hover 出现的
 * 播放键从那一帧起播。
 *
 * 看图优先，播放键不常驻（用户决策 2026-09-06）：桌面 hover 才浮出中央播放键；
 * 触摸屏没有 hover，直接点卡片进灯箱，灯箱顶栏有「从 xx:xx 播放」——大图与
 * 播放键都在灯箱里，小卡片上什么都不盖。
 *
 * 章节可能还没有图（后台正在抓 / 库关了开关 / ffmpeg 缺失）：卡片是深色占位 +
 * 时间戳，仍可点击跳播——章节列表本身就有用，图是附属物。
 */
export function ChapterStrip({
  chapters,
  pending,
  resumeMs,
  onPlay,
}: {
  chapters: LibraryChapter[];
  /** 场景图正在后台生成：右上角给个提示，占位卡不算"没有图" */
  pending: boolean;
  /** 当前观看者上次看到的位置（毫秒）；落在哪一章就在那张卡标「上次看到这里」 */
  resumeMs: number | null;
  /** 从章节起播；起播时间用图上那一帧的真实时间，无图时退回章节起点 */
  onPlay: (chapter: LibraryChapter) => void;
}) {
  const [lightbox, setLightbox] = useState<number | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const resumeScrolled = useRef(false);

  // 上次看到的位置落在哪一章：start ≤ pos < end（末章无 end 视为到片尾）
  const resumeIndex = useMemo(() => {
    if (resumeMs == null || resumeMs <= 0) return -1;
    return chapters.findIndex(
      (c) => resumeMs >= c.start_ms && (c.end_ms == null || resumeMs < c.end_ms),
    );
  }, [chapters, resumeMs]);

  // 首次知道续播章节时把它横滚到中间；用户之后自己滑不再抢位置
  useEffect(() => {
    if (resumeScrolled.current || resumeIndex < 0) return;
    const frame = window.requestAnimationFrame(() => {
      sectionRef.current
        ?.querySelector<HTMLElement>(`[data-chapter-index="${resumeIndex}"]`)
        ?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
      resumeScrolled.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [resumeIndex]);

  if (chapters.length === 0) return null;

  // 灯箱只放有图的章节；下标映射回章节
  const withImages = chapters.filter((c) => c.image_url);
  const lightboxImages = withImages.map((c) => imageUrl(c.image_url));
  const captions = withImages.map((c) => chapterCaption(c));

  const openLightbox = (chapter: LibraryChapter) => {
    const i = withImages.indexOf(chapter);
    if (i >= 0) setLightbox(i);
  };

  return (
    <section ref={sectionRef} aria-label="场景">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          场景
        </h2>
        <span className="tnum text-sub text-[var(--text-faint)]">
          {chapters[0].synthetic ? `${chapters.length} 个片段` : `${chapters.length} 个章节`}
        </span>
        {pending && (
          <span className="ml-auto flex items-center gap-2 text-caption text-[var(--text-muted)]">
            <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-white/20 border-t-white/70" />
            正在生成章节
          </span>
        )}
      </div>

      <HScroller className="-mx-1 gap-3 px-1 pb-1 pt-1">
        {chapters.map((chapter) => (
          <ChapterCard
            key={chapter.index}
            chapter={chapter}
            resumeHere={chapter.index === resumeIndex}
            onOpen={() => (chapter.image_url ? openLightbox(chapter) : onPlay(chapter))}
            onPlay={() => onPlay(chapter)}
          />
        ))}
      </HScroller>

      {lightbox != null && lightboxImages.length > 0 && (
        <ImageLightbox
          images={lightboxImages}
          captions={captions}
          initialIndex={lightbox}
          thumbAspect="landscape"
          action={{
            label: "从此处播放",
            busyLabel: "正在打开…",
            doneLabel: "已打开",
            icon: <PlayIcon className="size-3.5" />,
            run: async (index) => {
              const target = withImages[index];
              if (target) onPlay(target);
            },
          }}
          onClose={() => setLightbox(null)}
        />
      )}
    </section>
  );
}

/** 灯箱顶栏与卡片标题共用的章节说明："标题 · 12:30"，无标题只给时间。 */
function chapterCaption(chapter: LibraryChapter): string {
  const clock = formatClock(chapter.frame_ms ?? chapter.start_ms);
  return chapter.title ? `${chapter.title} · ${clock}` : clock;
}

function ChapterCard({
  chapter,
  resumeHere,
  onOpen,
  onPlay,
}: {
  chapter: LibraryChapter;
  resumeHere: boolean;
  onOpen: () => void;
  onPlay: () => void;
}) {
  const clock = formatClock(chapter.frame_ms ?? chapter.start_ms);
  const label = chapter.title ?? `第 ${chapter.index + 1} 段`;
  return (
    <div className="group/chapter w-[240px] shrink-0 max-md:w-[200px]" data-chapter-index={chapter.index}>
      {/* 画面区单独一个相对定位盒：播放键按它居中，不受下面标题行高度影响 */}
      <div className="relative aspect-video transition duration-200 group-hover/chapter:-translate-y-0.5">
        <button
          type="button"
          onClick={onOpen}
          aria-label={chapter.image_url ? `查看场景图：${label} ${clock}` : `从 ${clock} 播放`}
          className="relative block size-full overflow-hidden rounded-xl bg-[#141824] text-left outline-none ring-1 ring-white/[0.08] transition duration-200 hover:ring-white/35 focus-visible:ring-2 focus-visible:ring-white/70"
        >
          <PosterImage
            src={imageUrl(chapter.image_url, "landscape-card")}
            alt={`${label} 场景图`}
            className="size-full object-cover"
            fallback={
              <span className="tnum flex size-full items-center justify-center text-[20px] font-bold text-white/20">
                {clock}
              </span>
            }
          />
          {/* 左下角时间戳：任何画面上都要能读，压一层暗底 */}
          <span className="tnum text-on-image pointer-events-none absolute bottom-1.5 left-1.5 rounded bg-black/60 px-1.5 py-px text-micro font-semibold text-white/90">
            {clock}
          </span>
          {resumeHere && (
            <span className="pointer-events-none absolute right-1.5 top-1.5 rounded bg-[var(--accent-2)] px-1.5 py-px text-micro font-semibold text-white shadow-lg">
              上次看到这里
            </span>
          )}
        </button>

        {/* 中央播放键：桌面 hover 才出现，触摸屏（没有 hover）不渲染——看图优先，
            小卡片上不盖东西；触屏用户点卡片进灯箱，灯箱里有播放。 */}
        {chapter.image_url && (
          <button
            type="button"
            aria-label={`从 ${clock} 播放`}
            onClick={onPlay}
            className="absolute left-1/2 top-1/2 flex size-11 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-[1.5px] border-white/80 bg-black/35 text-white opacity-0 shadow-[0_1px_10px_rgba(0,0,0,0.45)] transition duration-200 hover:scale-[1.06] hover:border-white hover:bg-black/50 focus-visible:opacity-100 focus-visible:outline-none group-hover/chapter:opacity-100 [@media(hover:none)]:hidden"
          >
            <PlayIcon className="size-8 drop-shadow-[0_1px_2px_rgba(0,0,0,0.55)]" />
          </button>
        )}
      </div>

      <p className="mt-2 truncate text-ui font-medium text-[var(--text)]" title={label}>
        {label}
      </p>
    </div>
  );
}
