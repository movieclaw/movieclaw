"use client";

import { useEffect, useState } from "react";
import type { Route } from "next";

import { PosterCardVisual } from "@/components/poster-card";
import type { MediaSearchItem } from "@/lib/api/discover";
import { getTitleSearchHistoryResults, searchTitles } from "@/lib/api/search";
import { formatRelativeTime } from "@/lib/time";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/**
 * 搜索结果页「影视」垂直：豆瓣 + TMDB 双来源搜索，上下两个分区展示。
 *
 * 与站点资源垂直（SearchResults）并列挂在 /search 页的选项卡下。后端一次
 * 并行搜索两个来源并返回各自状态：豆瓣中文条目全、TMDB 对外语片/动画覆盖更好且
 * 自带年份和类型。两边没有可靠的对齐键（豆瓣轻量结果无 IMDB ID），不做合并
 * 去重——分区并列反而让用户一眼对比。单边失败/为空只在该分区内提示（TMDB
 * 未配置 Key 时的引导也走这里），不拖垮另一边。
 *
 * 两种数据来源：
 *   - 实时搜索（snapshotId 为空）：统一搜索两个来源并只保存一条历史；
 *   - 快照回放（snapshotId 非空，点历史进入）：读历史留存的结果快照，
 *     不访问上游，头部出
 *     「X 前的快照 · 重新搜索」提示；快照缺失（被清理/老数据）回退实时搜索。
 *
 * 空态是媒体优先设计的关键出口：用户搜软件名等非影视关键词时两边都会空手，
 * 必须给一个显眼的「去站点资源搜索」入口，否则用户会以为搜索坏了。
 */
export function MediaSearchResults({
  keyword,
  snapshotId,
  onResearch,
  onSwitchToTorrent,
}: {
  keyword: string;
  /** 非空 = 回放该条历史的媒体结果快照，而非发起实时搜索 */
  snapshotId?: number;
  /** 快照提示条的「重新搜索」：切回实时搜索（丢掉 snapshot 参数）；不传则不渲染该按钮 */
  onResearch?: () => void;
  /** 切到「站点资源」垂直（空态/出错时的逃生入口） */
  onSwitchToTorrent?: () => void;
}) {
  const scrollRef = useScrollRestoration(`search:media:${keyword}:${snapshotId ?? "live"}`);
  // 每个来源各自三态：null = 加载中；[] = 无结果；error 非空 = 该分区失败
  const [douban, setDouban] = useState<MediaSearchItem[] | null>(null);
  const [doubanError, setDoubanError] = useState<string | null>(null);
  const [tmdb, setTmdb] = useState<MediaSearchItem[] | null>(null);
  const [tmdbError, setTmdbError] = useState<string | null>(null);
  // 快照回放态：非空 = 当前展示的是历史快照（值为快照生成时间，供提示条换算年龄）
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setDouban(null);
    setDoubanError(null);
    setTmdb(null);
    setTmdbError(null);
    setSnapshotAt(null);

    // 实时搜索：一个语义化请求返回双来源结果与独立状态，单边失败不丢另一边。
    const searchLive = () => {
      return searchTitles(
        keyword,
        { provider: "all", saveHistory: true },
        { signal: controller.signal },
      ).then(({ items, providers }) => {
        if (controller.signal.aborted) return;
        setDouban(items.filter((item) => item.source === "douban"));
        setTmdb(items.filter((item) => item.source === "tmdb"));
        const doubanStatus = providers.find((status) => status.provider === "douban");
        const tmdbStatus = providers.find((status) => status.provider === "tmdb");
        if (doubanStatus && !doubanStatus.success) setDoubanError(doubanStatus.message ?? "豆瓣搜索失败");
        if (tmdbStatus && !tmdbStatus.success) setTmdbError(tmdbStatus.message ?? "TMDB 搜索失败");
      });
    };

    // 快照回放：新快照含双来源；旧快照只有豆瓣，按 source 分组即可自然兼容。
    const load =
      snapshotId != null
        ? getTitleSearchHistoryResults(snapshotId)
            .then((snap) => {
              if (controller.signal.aborted) return;
              const items = snap.items;
              setSnapshotAt(snap.snapshot_at);
              setDouban(items.filter((item) => item.source === "douban"));
              setTmdb(items.filter((item) => item.source === "tmdb"));
            })
            .catch(() => searchLive())
        : searchLive();

    load.catch((reason: Error) => {
      if (controller.signal.aborted) return;
      const message = reason.message || "影视搜索失败，请稍后重试";
      setDoubanError(message);
      setTmdbError(message);
    });
    return () => controller.abort();
  }, [keyword, snapshotId]);

  // 两个分区都落定后才能判定「全空」；快照与实时结果遵循同一展示规则。
  const doubanSettled = douban !== null || doubanError !== null;
  const tmdbSettled = tmdb !== null || tmdbError !== null;
  const hasAnyResult = (douban?.length ?? 0) > 0 || (tmdb?.length ?? 0) > 0;
  const allEmpty = doubanSettled && tmdbSettled && !hasAnyResult;
  const anyError = doubanError ?? tmdbError;

  return (
    <div className="relative flex h-full flex-col">
      {/* 状态行：与站点资源垂直的头部同构（关键词 + 快照提示） */}
      <header className={`shrink-0 pb-3 pt-4 page-inset max-md:pt-3`}>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
          <h1 className="text-on-image text-title-lg font-semibold tracking-[-0.01em] text-white">
            “{keyword}”
          </h1>

          {/* 快照提示：药丸 + 重新搜索（与站点资源垂直同款视觉） */}
          {snapshotAt && (
            <div className="ml-auto flex items-center gap-2">
              <span
                title="这是历史留存的结果快照，来源站数据（评分/海报）可能已变化"
                className="flex items-center gap-1.5 rounded-full border border-[var(--info-soft)]/30 bg-[var(--info-soft)]/12 px-2.5 py-1 text-caption text-[var(--info-text)] backdrop-blur-sm"
              >
                <svg
                  viewBox="0 0 24 24"
                  className="size-[13px] shrink-0"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.8}
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="12" r="8.5" />
                  <path d="M12 7.5V12l3 2" />
                </svg>
                {formatRelativeTime(snapshotAt)}的快照
              </span>
              {onResearch && (
                <button
                  type="button"
                  onClick={onResearch}
                  className="btn-accent rounded-full px-2.5 py-1 text-caption font-medium"
                >
                  重新搜索
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      <div
        ref={scrollRef}
        className={`scroll-thin scroll-safe relative min-h-0 flex-1 overflow-y-auto pb-6 page-inset`}
      >
        {allEmpty ? (
          <MediaSearchEmpty
            title={anyError ? "影视搜索出错" : "没有找到相关影视条目"}
            hint={anyError ?? "换个关键词试试；如果找的是非影视资源，可以直接搜索站点。"}
            onSwitchToTorrent={onSwitchToTorrent}
          />
        ) : (
          <div className="flex flex-col gap-7">
            <MediaSourceSection
              label="豆瓣"
              items={douban}
              error={doubanError}
              hrefOf={(item) => `/media/douban/${item.id}` as Route}
            />
            <MediaSourceSection
              label="TMDB"
              items={tmdb}
              error={tmdbError}
              hrefOf={(item) => `/media/${item.type ?? "movie"}/${item.id}` as Route}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/** 单个来源分区：来源徽标 + 计数子标题，正文按「加载 / 出错 / 空 / 网格」四态渲染。 */
function MediaSourceSection({
  label,
  items,
  error,
  hrefOf,
}: {
  label: string;
  /** null = 加载中 */
  items: MediaSearchItem[] | null;
  error: string | null;
  hrefOf: (item: MediaSearchItem) => Route;
}) {
  return (
    <section>
      <div className="mb-3 flex items-center gap-2.5">
        <span className="rounded-full bg-black/30 px-2.5 py-0.5 text-caption text-[var(--accent)] backdrop-blur-sm">
          {label}
        </span>
        {items && items.length > 0 && (
          <span className="text-on-image text-sub text-[rgba(243,245,249,0.75)]">
            共 {items.length} 条结果
          </span>
        )}
      </div>

      {!items && !error && <MediaSearchSkeleton />}
      {error && (
        <p className="text-on-image text-sub leading-relaxed text-[rgba(243,245,249,0.6)]">
          {error}
        </p>
      )}
      {items?.length === 0 && (
        <p className="text-on-image text-sub text-[rgba(243,245,249,0.6)]">
          该来源没有找到相关条目
        </p>
      )}
      {items && items.length > 0 && (
        <div className="grid gap-x-4 gap-y-7 pt-1 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))]">
          {items.map((item) => (
            <div key={item.id} className="min-w-0">
              <PosterCardVisual item={item} href={hrefOf(item)} />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** 空态/错误态：说明文案 + 「搜索站点资源」逃生按钮。 */
function MediaSearchEmpty({
  title,
  hint,
  onSwitchToTorrent,
}: {
  title: string;
  hint: string;
  onSwitchToTorrent?: () => void;
}) {
  return (
    <div className="flex flex-col items-center pt-24 text-center">
      <p className="text-on-image text-body-lg font-semibold text-white">{title}</p>
      <p className="text-on-image mt-1.5 text-sub text-[rgba(243,245,249,0.7)]">{hint}</p>
      {onSwitchToTorrent && (
        <button
          type="button"
          onClick={onSwitchToTorrent}
          className="btn-accent mt-5 rounded-full px-4 py-1.5 text-sub font-semibold"
        >
          搜索站点资源
        </button>
      )}
    </div>
  );
}

function MediaSearchSkeleton() {
  return (
    <div className="grid gap-x-4 gap-y-7 pt-1 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))]">
      {Array.from({ length: 7 }, (_, index) => (
        <div key={index} className="aspect-[2/3] animate-pulse rounded-2xl bg-white/[0.05]" />
      ))}
    </div>
  );
}
