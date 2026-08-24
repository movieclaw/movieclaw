"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";

import { VideoPlayer } from "@/components/player/video-player";
import {
  type PlaybackItemInfo,
  getPlaybackItem,
  getPlaybackItemEpisodes,
} from "@/lib/api/playback";
import type { LibraryEpisode } from "@/lib/api/libraries";
import { imageUrl } from "@/lib/image-proxy";
import { playerReturnPath } from "@/lib/player/play-links";

/**
 * 播放页的数据装配层（docs/design/web-player.md §6.10）。
 *
 * 与 `VideoPlayer` 分开的理由：播放器只认「播放单元 + 展示文案」，不该知道
 * 媒体库、季集清单这些东西的存在。这一层负责把季集结构翻译成播放单元，
 * 以及算出「下一集是谁」。
 *
 * **任何数据都不挡起播**。播放单元从地址就能算出来，VideoPlayer 立即挂载、
 * 立即发会话请求；条目信息（片名/海报/库归属）和分集清单并行加载，回来了
 * 再补进画面。以前是「先等详情、再开始一切」——纯黑转圈屏白等一整个网络
 * 往返，正是首帧延迟里最冤的一段。条目信息只有一个地方非要不可：退出播放
 * 的兜底落点（条目详情页要 library_id），还没回来就退回媒体库首页。
 */

export interface PlayerPageProps {
  mediaItemId: number;
  /** 剧集的起播季集；电影两者都不给 */
  season?: number;
  episode?: number;
  /** 分享链接的 `?t=` 起播覆盖（毫秒）；不给 = 服务端接各自的续播点 */
  startMsOverride?: number;
}

/** SxxExx。季集号补零是媒体库的通用写法，别自创。 */
function episodeCode(season: number, episode: number): string {
  return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
}

export function PlayerPage({ mediaItemId, season, episode, startMsOverride }: PlayerPageProps) {
  const router = useRouter();
  const [info, setInfo] = useState<PlaybackItemInfo | null>(null);
  const [episodes, setEpisodes] = useState<LibraryEpisode[]>([]);
  const [current, setCurrent] = useState<{ season: number; episode: number } | null>(
    season !== undefined && episode !== undefined ? { season, episode } : null,
  );
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPlaybackItem(mediaItemId)
      .then((loaded) => {
        if (!cancelled) setInfo(loaded);
      })
      .catch((error: unknown) =>
        // 条目信息拿不到几乎必然意味着会话也开不了（同一套可见性判据），
        // 这里给出人话；真正的播放错误由播放器自己的错误页负责
        setFailed(error instanceof Error ? error.message : "读取条目信息失败"),
      );
    return () => {
      cancelled = true;
    };
  }, [mediaItemId]);

  // 剧集才拉分集清单：它只用来算「下一集」和标题里的集名，拿不到不影响播放
  const activeSeason = current?.season;
  useEffect(() => {
    if (activeSeason === undefined) return;
    let cancelled = false;
    getPlaybackItemEpisodes(mediaItemId, activeSeason)
      .then((loaded) => {
        if (!cancelled) setEpisodes(loaded.episodes);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [mediaItemId, activeSeason]);

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
    // 「回哪去」按优先级：进来前记的路径（sessionStorage，见 play-links.ts，
    // 分享链接的接收者没有）> 条目详情页（要 library_id，条目信息已回来）>
    // 媒体库首页兜底
    const remembered = playerReturnPath();
    const fallback = info ? `/library/${info.library_id}/item/${mediaItemId}` : "/library";
    router.replace((remembered ?? fallback) as Route);
  }, [router, info, mediaItemId]);

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

  return (
    <VideoPlayer
      unit={unit}
      title={info?.title ?? null}
      episodeLabel={
        current
          ? `${episodeCode(current.season, current.episode)}${
              currentEpisode?.name ? ` · ${currentEpisode.name}` : ""
            }`
          : null
      }
      posterUrl={info?.poster_url ? imageUrl(info.poster_url) : null}
      startMsOverride={startMsOverride}
      next={next}
      prev={prev}
      onPlayNext={playNext}
      onPlayPrev={playPrev}
      onExit={exit}
    />
  );
}
