"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";

import { VideoPlayer } from "@/components/player/video-player";
import {
  type LibraryEpisode,
  type LibraryItemDetail,
  getItemEpisodes,
  getLibraryItemDetail,
} from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";

/**
 * 播放页的数据装配层。
 *
 * 与 `VideoPlayer` 分开的理由：播放器只认「播放单元 + 展示文案」，不该知道
 * 媒体库、季集清单这些东西的存在——否则以后从别的入口（收藏、搜索结果、
 * 外部深链）起播就要把整套库接口一起拖进去。这一层负责把库里的季集结构
 * 翻译成播放单元，以及算出「下一集是谁」。
 */

export interface PlayerPageProps {
  libraryId: number;
  mediaItemId: number;
  /** 剧集的起播季集；电影两者都不给 */
  season?: number;
  episode?: number;
  /** 退出播放后回到哪里；缺省回条目详情页 */
  returnTo?: string;
}

/** SxxExx。季集号补零是媒体库的通用写法，别自创。 */
function episodeCode(season: number, episode: number): string {
  return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
}

export function PlayerPage({ libraryId, mediaItemId, season, episode, returnTo }: PlayerPageProps) {
  const router = useRouter();
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  const [episodes, setEpisodes] = useState<LibraryEpisode[]>([]);
  const [current, setCurrent] = useState<{ season: number; episode: number } | null>(
    season !== undefined && episode !== undefined ? { season, episode } : null,
  );
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getLibraryItemDetail(libraryId, mediaItemId)
      .then((loaded) => {
        if (!cancelled) setDetail(loaded);
      })
      .catch((error: unknown) =>
        setFailed(error instanceof Error ? error.message : "读取条目信息失败"),
      );
    return () => {
      cancelled = true;
    };
  }, [libraryId, mediaItemId]);

  // 剧集才拉分集清单：它只用来算「下一集」和标题里的集名，拿不到不影响播放
  const activeSeason = current?.season;
  useEffect(() => {
    if (activeSeason === undefined) return;
    let cancelled = false;
    getItemEpisodes(libraryId, mediaItemId, activeSeason)
      .then((loaded) => {
        if (!cancelled) setEpisodes(loaded.episodes);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [libraryId, mediaItemId, activeSeason]);

  const unit = useMemo(
    () => ({
      media_item_id: mediaItemId,
      // 电影用 (0, 0) 哨兵，与台账、playback_state 的约定一致
      season_number: current?.season ?? 0,
      episode_number: current?.episode ?? 0,
    }),
    [mediaItemId, current?.season, current?.episode],
  );

  const currentEpisode = current
    ? (episodes.find((item) => item.episode_number === current.episode) ?? null)
    : null;

  /** 下一集只在**本季且有在位文件**里找。缺集要跳过，不能停在一个放不了的单元上。 */
  const next = useMemo(() => {
    if (!current) return null;
    const candidate = episodes
      .filter((item) => item.episode_number > current.episode && item.owned)
      .sort((a, b) => a.episode_number - b.episode_number)[0];
    if (!candidate) return null;
    return {
      unit: {
        media_item_id: mediaItemId,
        season_number: current.season,
        episode_number: candidate.episode_number,
      },
      label: `${episodeCode(current.season, candidate.episode_number)}${
        candidate.name ? ` · ${candidate.name}` : ""
      }`,
    };
  }, [episodes, current, mediaItemId]);

  /** 上一集：同样只在**本季且有在位文件**里找，规则与「下一集」对称。 */
  const prev = useMemo(() => {
    if (!current) return null;
    const candidate = episodes
      .filter((item) => item.episode_number < current.episode && item.owned)
      .sort((a, b) => b.episode_number - a.episode_number)[0];
    if (!candidate) return null;
    return {
      unit: {
        media_item_id: mediaItemId,
        season_number: current.season,
        episode_number: candidate.episode_number,
      },
      label: `${episodeCode(current.season, candidate.episode_number)}${
        candidate.name ? ` · ${candidate.name}` : ""
      }`,
    };
  }, [episodes, current, mediaItemId]);

  const playNext = useCallback(() => {
    if (!next) return;
    setCurrent({
      season: next.unit.season_number ?? 0,
      episode: next.unit.episode_number ?? 0,
    });
  }, [next]);

  const playPrev = useCallback(() => {
    if (!prev) return;
    setCurrent({
      season: prev.unit.season_number ?? 0,
      episode: prev.unit.episode_number ?? 0,
    });
  }, [prev]);

  const exit = useCallback(() => {
    const fallback = `/library/${libraryId}/item/${mediaItemId}`;
    router.replace((returnTo ?? fallback) as Route);
  }, [router, returnTo, libraryId, mediaItemId]);

  if (failed) {
    return (
      <div className="flex size-full items-center justify-center bg-black px-6 text-center">
        <div>
          <p className="text-[15px] text-white">{failed}</p>
          <button
            type="button"
            onClick={exit}
            className="mt-4 rounded-xl bg-white/10 px-4 py-2 text-[13px] text-white transition-colors hover:bg-white/15"
          >
            返回
          </button>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex size-full items-center justify-center bg-black">
        <span className="size-8 animate-spin rounded-full border-2 border-white/25 border-t-white" />
      </div>
    );
  }

  return (
    <VideoPlayer
      unit={unit}
      title={detail.title}
      episodeLabel={
        current
          ? `${episodeCode(current.season, current.episode)}${
              currentEpisode?.name ? ` · ${currentEpisode.name}` : ""
            }`
          : null
      }
      posterUrl={detail.poster_url ? imageUrl(detail.poster_url) : null}
      next={next}
      prev={prev}
      onPlayNext={playNext}
      onPlayPrev={playPrev}
      onExit={exit}
    />
  );
}
