"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import type { Route } from "next";
import { useRouter } from "next/navigation";

import { CastRow } from "@/components/cast-row";
import { ChapterStrip } from "@/components/chapter-strip";
import {
  ExpandablePlot,
  PlayAction,
  SeasonEpisodesSection,
  type SelectedEpisodeContext,
  SourceLink,
} from "@/components/library-item-detail-view";
import { ReadOnlyTrackRows } from "@/components/media-track-rows";
import { type SharedFile, type SharedItem, getSharedEpisodes, getSharedItem } from "@/lib/api/shares";
import {
  type PlaybackUnit,
  type PlaybackWatchState,
  fetchResumeState,
  sharePlaybackScope,
} from "@/lib/api/playback";
import { publicEnv } from "@/lib/env";
import { formatBytes, formatRuntimeMinutes, formatVideoResolution } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { HttpError } from "@/lib/http";
import { expiryHint, sharePlayPath } from "@/lib/share";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 分享页的影片页（docs/design/media-share.md §1.2）：详情页的浏览面，去掉一切
 * 管理与站内入口。剧照铺底、标题与规格、简介、（剧集）季集横滚、章节条、
 * 演职员——播放走 /s/{slug}/play。
 *
 * 与 LibraryItemDetailView 共用 PlayAction / SeasonEpisodesSection / ExpandablePlot
 * 三个纯展示组件；其余（音轨编辑、文件区、⋯ 菜单）访客用不上，不复用。
 */
export function SharedItemView({
  slug,
  mediaItemId,
  onBack,
}: {
  slug: string;
  /** 合集分享时看哪一部；条目分享不传（范围就那一个） */
  mediaItemId?: number;
  /** 合集分享里给一条回名单的路；条目分享没有"上一层" */
  onBack?: () => void;
}) {
  const router = useRouter();
  const [item, setItem] = useState<SharedItem | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectedEpisodeContext<SharedFile> | null>(null);
  const [watched, setWatched] = useState<PlaybackWatchState | null>(null);
  const scope = useMemo(() => sharePlaybackScope(slug), [slug]);

  useEffect(() => {
    let cancelled = false;
    getSharedItem(slug, mediaItemId)
      .then((data) => {
        if (!cancelled) setItem(data);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setFailed(
          error instanceof HttpError
            ? error.message || "读取影片信息失败"
            : "无法连接到服务器，请检查网络后重试",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [slug, mediaItemId]);

  usePageTitle(item?.title);

  const isMovie = item ? item.kind !== "tv" : true;
  const playUnit = useMemo<PlaybackUnit | null>(() => {
    if (!item) return null;
    if (item.kind !== "tv") {
      return { media_item_id: item.media_item_id, season_number: 0, episode_number: 0 };
    }
    if (!selected) return null;
    return {
      media_item_id: item.media_item_id,
      season_number: selected.seasonNumber,
      episode_number: selected.episode.episode_number,
    };
  }, [item, selected]);
  const playUnitKey = playUnit
    ? `${playUnit.media_item_id}/${playUnit.season_number}/${playUnit.episode_number}`
    : null;

  // 续播点只在本浏览器（scope.progress=local）：换浏览器就从头看
  useEffect(() => {
    if (!playUnit) {
      setWatched(null);
      return;
    }
    let cancelled = false;
    fetchResumeState(playUnit, scope)
      .then((state) => {
        if (!cancelled) setWatched(state);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // playUnit 按内容（三元组）比较
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playUnitKey, scope]);

  const fetchEpisodes = useCallback(
    (_mediaItemId: number, season: number) => getSharedEpisodes(slug, season, mediaItemId),
    [slug, mediaItemId],
  );

  const play = useCallback(
    (tSeconds?: number) => {
      if (!item) return;
      router.push(
        sharePlayPath(slug, {
          season: !isMovie && selected ? selected.seasonNumber : undefined,
          episode: !isMovie && selected ? selected.episode.episode_number : undefined,
          tSeconds,
        }) as Route,
      );
    },
    [item, isMovie, router, selected, slug],
  );

  if (failed) {
    return (
      <div className="flex min-h-dvh items-center justify-center px-6 text-center">
        <p className="text-body text-white/80">{failed}</p>
      </div>
    );
  }
  if (!item) {
    return (
      <div className="flex min-h-dvh items-center justify-center gap-2.5 text-ui text-white/60">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在读取影片信息…
      </div>
    );
  }

  const meta = item.local_meta;
  const trackFiles = isMovie ? item.files : (selected?.files ?? []);
  const availableFiles = trackFiles.filter((file) => file.state === "in_place");
  const currentFile = availableFiles[0] ?? null;
  // 片长：NFO 优先，其次任意文件的实测时长；不足一分钟的短片不显示，免得出现「—」
  const runtimeMinutes =
    meta?.runtime_minutes ||
    (() => {
      const probed = item.files.find((f) => f.duration_seconds)?.duration_seconds;
      return probed && probed >= 60 ? Math.round(probed / 60) : null;
    })();
  const resolutions = [
    ...new Set(
      availableFiles
        .map((file) => file.resolution)
        .filter((resolution): resolution is string => Boolean(resolution)),
    ),
  ]
    .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
    .map(formatVideoResolution);
  const hdrFormats = [
    ...new Set(availableFiles.map((file) => file.hdr).filter((hdr): hdr is string => Boolean(hdr))),
  ];
  const facts = [
    item.year ? String(item.year) : null,
    runtimeMinutes != null ? formatRuntimeMinutes(runtimeMinutes) : null,
    resolutions.length > 0 ? resolutions.join(" / ") : null,
    hdrFormats.length > 0 ? hdrFormats.join(" / ") : null,
  ].filter((fact): fact is string => Boolean(fact));
  const plot = isMovie ? meta?.plot : (selected?.episode.overview ?? meta?.plot);
  const cast = meta
    ? [
        ...(meta.director_credits.length > 0
          ? meta.director_credits.map((director) => ({
              name: director.name,
              credit: "导演",
              avatarUrl: director.thumb_url ? imageUrl(director.thumb_url) : null,
            }))
          : [...new Set(meta.directors)].map((name) => ({ name, credit: "导演" }))),
        ...meta.actors.map((actor) => ({
          name: actor.name,
          role: actor.role,
          avatarUrl: actor.thumb_url ? imageUrl(actor.thumb_url) : null,
        })),
      ]
    : [];
  const backdrop = imageUrl(item.backdrop_url ?? item.poster_url);
  const canPlay = availableFiles.length > 0 && (isMovie || selected !== null);

  return (
    <div className="relative min-h-dvh">
      {/* 剧照铺底：固定在视口，内容从下方的渐变板上浮出 */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 bg-cover bg-center"
        style={backdrop ? { backgroundImage: `url("${backdrop}")` } : undefined}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 bg-[linear-gradient(to_bottom,rgba(7,8,12,0.15)_0%,rgba(7,8,12,0.75)_45%,#07080c_75%)]"
      />

      <div className="relative z-10">
        {/* 顶栏只有字标与到期提示：没有登录入口、没有搜索、没有侧栏 */}
        <header className="flex items-center justify-between px-12 pt-5 max-md:px-4 max-md:pt-4">
          <div className="flex min-w-0 items-center gap-3">
            {/* 合集分享才有"上一层"：条目分享的范围就那一部，回不到别处去 */}
            {onBack && (
              <button
                type="button"
                onClick={onBack}
                className="shrink-0 rounded-lg px-2 py-1 text-sub text-white/60 transition hover:bg-white/10 hover:text-white"
              >
                ‹ 返回合集
              </button>
            )}
            <span className="text-sub font-semibold uppercase tracking-[0.18em] text-white/70">
              {publicEnv.appName}
            </span>
          </div>
          <span className="tnum text-caption text-white/55">链接 {expiryHint(item.expires_at)}</span>
        </header>

        <div className="h-[28vh] min-h-[160px] max-md:h-[20vh] max-md:min-h-[110px]" />

        {/* 底部留足呼吸：最后一段内容不贴屏幕底边，手机端再加安全区 */}
        <div className="px-12 pb-24 [padding-bottom:calc(6rem+var(--safe-bottom))] max-md:px-4">
          <div className="max-w-5xl">
            <h1 className="text-on-image text-[42px] font-bold leading-[1.1] tracking-[-0.02em] text-white max-md:text-[28px]">
              {item.title}
            </h1>
            {!isMovie && selected && (
              <p className="text-on-image mt-2 text-body text-white/65 max-md:mt-1.5 max-md:text-ui">
                {`第 ${selected.seasonNumber} 季 第 ${selected.episode.episode_number} 集${
                  selected.episode.name ? ` - ${selected.episode.name}` : ""
                }`}
              </p>
            )}
            {facts.length > 0 && (
              <p className="tnum mt-3.5 text-ui text-white/80 max-md:mt-2 max-md:text-sub">
                {facts.join(" · ")}
              </p>
            )}
            {meta && meta.genres.length > 0 && (
              <p className="text-on-image mt-3 text-ui leading-6 text-white/72 max-md:mt-2 max-md:text-sub">
                {meta.genres.join(" · ")}
              </p>
            )}
            {currentFile && (
              <p className="tnum mt-2 text-caption text-white/50">
                {[
                  currentFile.container?.toUpperCase(),
                  currentFile.video_codec?.toUpperCase(),
                  formatBytes(currentFile.size_bytes),
                  availableFiles.length > 1 ? `共 ${availableFiles.length} 个版本` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
            {/* 音轨 / 字幕：与详情页同一套分组与折叠，只读（无预览 / 删除 / 生成） */}
            {currentFile && (
              <ReadOnlyTrackRows
                key={currentFile.id}
                audioStreams={currentFile.audio_streams}
                subtitleStreams={currentFile.subtitle_streams}
              />
            )}

            {canPlay ? (
              <PlayAction watched={watched} onPlay={() => play()} />
            ) : (
              <p className="mt-5 text-ui text-white/55">
                {isMovie || selected ? "暂时没有可播放的文件。" : "请选择一集。"}
              </p>
            )}
          </div>

          {plot && (
            <div className="mt-4">
              <ExpandablePlot text={plot} />
            </div>
          )}

          <div className="mt-9 space-y-8 max-md:mt-6 max-md:space-y-6">
            {!isMovie && item.seasons.length > 0 && (
              <SeasonEpisodesSection
                libraryId={0}
                detail={{
                  media_item_id: item.media_item_id,
                  file_count: item.files.length,
                  seasons: item.seasons,
                  files: item.files,
                }}
                onEpisodeChange={setSelected}
                fetchEpisodes={fetchEpisodes}
              />
            )}

            {currentFile?.chapters && currentFile.chapters.length > 0 && (
              <ChapterStrip
                chapters={currentFile.chapters}
                pending={false}
                resumeMs={watched && !watched.played ? watched.position_ms : null}
                onPlay={(chapter) => play((chapter.frame_ms ?? chapter.start_ms) / 1000)}
              />
            )}

            {cast.length > 0 && <CastRow cast={cast} />}

            {/* 外部词条：与详情页同款，固定在最后；新窗口打开，不是站内入口 */}
            {(item.tmdb_id || item.imdb_id || item.douban_id) && (
              <div
                aria-label="外部词条"
                className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-white/[0.04] pt-4 text-caption"
              >
                <span className="text-[var(--text-faint)]">相关链接</span>
                {item.tmdb_id ? (
                  <SourceLink
                    href={`https://www.themoviedb.org/${item.kind === "tv" ? "tv" : "movie"}/${item.tmdb_id}`}
                    label="TMDB"
                  />
                ) : null}
                {item.imdb_id && (
                  <SourceLink href={`https://www.imdb.com/title/${item.imdb_id}/`} label="IMDb" />
                )}
                {item.douban_id && (
                  <SourceLink
                    href={`https://movie.douban.com/subject/${item.douban_id}/`}
                    label="豆瓣"
                  />
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
