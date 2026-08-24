"use client";

import type { Route } from "next";
import Link from "next/link";

import { HScroller } from "@/components/h-scroller";
import { CheckIcon, PlayIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import type { RecentWatchItem } from "@/lib/api/playback";
import { playHref, rememberPlayerReturnPath } from "@/lib/player/play-links";
import { imageUrl } from "@/lib/image-proxy";
import { formatRelativeTime } from "@/lib/time";
import { useTapGuard } from "@/lib/use-tap-guard";

/** S1E2 统一展示为播放器常见的 S01E02，扫一眼即可辨认具体集。 */
function episodeCode(item: RecentWatchItem): string {
  return `S${String(item.season_number).padStart(2, "0")}E${String(item.episode_number).padStart(2, "0")}`;
}

function itemHref(item: RecentWatchItem): Route {
  const base = `/library/${item.library_id}/item/${item.media_item_id}`;
  if (item.kind === "tv") {
    return `${base}?season=${item.season_number}&episode=${item.episode_number}&from=recent` as Route;
  }
  return `${base}?from=recent` as Route;
}

/**
 * 卡片中央播放键的落点：直接进播放页，不经过条目详情。
 *
 * 不需要在这里带上「从哪一秒开始」——续播点、上次用的音轨都由服务端在
 * 开会话时按观看状态解析（§6.10）。前端再算一次只会算出两个版本的真相。
 *
 * 退出回哪不进地址（分享者的上下文不是内容标识），点击时记进
 * sessionStorage；这张卡只出现在媒体库首页，落点固定。
 */
function cardPlayHref(item: RecentWatchItem): Route {
  return playHref(item.media_item_id, {
    season: item.kind === "tv" ? item.season_number : undefined,
    episode: item.kind === "tv" ? item.episode_number : undefined,
  }) as Route;
}

/** 播放键的读法：看完的是重播，看了一半的是续播，其余就是播放。 */
function playVerb(item: RecentWatchItem): string {
  if (item.played) return "重新播放";
  return item.position_ms > 0 ? "继续播放" : "播放";
}

/** 毫秒 → 播放器风格时钟，只有超过一小时才带小时位，短片保持 12:34 的紧凑写法。 */
function clock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  if (hours <= 0) return `${minutes}:${seconds}`;
  return `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`;
}

/**
 * 「已播 / 总时长」文本。只在"看了一半"时出现：
 * 已看完的作品右上角有对勾就够了，尚未开播或后端没探到时长的也不必占位，
 * 卡片上始终只保留当下最有用的那一条信息。
 */
function playbackClock(item: RecentWatchItem): string | null {
  if (item.played || item.position_ms <= 0 || !item.duration_ms) return null;
  return `${clock(item.position_ms)} / ${clock(item.duration_ms)}`;
}

/**
 * 卡片底部第三行的状态文案。卡片上已经用时钟展示进度时不再重复百分比，
 * 这一行只回答"什么时候看的"。
 */
function stateLabel(item: RecentWatchItem, hasClock: boolean): string {
  const ago = formatRelativeTime(item.last_played_at);
  if (item.played) return `${ago}已看完`;
  if (item.position_ms > 0 && item.progress_percent != null && !hasClock) {
    return `${ago}观看到 ${item.progress_percent}%`;
  }
  if (item.position_ms > 0) return `${ago}观看过`;
  return `${ago}播放过`;
}

/** 媒体库首页顶部的最近观看横排；空列表整段隐藏。 */
export function RecentWatchRow({ items }: { items: RecentWatchItem[] | null }) {
  // 首页不存在观看记录时完全不占位；首次请求尚未返回也先保持原布局，
  // 避免从未使用播放器的用户看到一个没有实际内容的分区骨架。
  if (!items?.length) return null;

  return (
    <section className="mt-8 max-md:mt-6" aria-labelledby="recent-watch-title">
      <div className="px-6 max-md:px-4">
        <h3
          id="recent-watch-title"
          className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
        >
          最近观看
        </h3>
      </div>
      <HScroller className="mt-3 gap-4 px-6 pb-2 pt-1 max-md:gap-3 max-md:px-4">
        {items.map((item) => <RecentWatchCard key={item.media_item_id} item={item} />)}
      </HScroller>
    </section>
  );
}

function RecentWatchCard({ item }: { item: RecentWatchItem }) {
  const tapGuard = useTapGuard();
  // 播放键自己也要过一遍误触保护：横滑最近观看这一行时，手指常常正好停在
  // 卡片中央，没有它一划就起播了（比误进详情页糟得多——会真的起转码会话）。
  // onTap 顺带记「退出回媒体库首页」：guard 自己接管 onClick，另挂会被覆盖
  const playTapGuard = useTapGuard(() => rememberPlayerReturnPath("/library"));
  const isEpisode = item.kind === "tv";
  const code = isEpisode ? episodeCode(item) : null;
  // 集号不再压在剧照左上角（会跟角标、进度挤在一起），改到片名下方的副标题里，
  // 与分集标题连读成「S01E02 · 集名」，海报本身保持干净。
  const context = isEpisode
    ? [code, item.episode_title].filter(Boolean).join(" · ")
    : item.year
      ? String(item.year)
      : "";
  const progress = item.played ? 100 : item.progress_percent;
  const timeLabel = playbackClock(item);
  const artworkUrl = isEpisode ? item.episode_still_url : item.backdrop_url;
  // 角标回答“还能接着看几集”：已经看过的集不计，因此整部剧看完后角标自然消失。
  const unwatchedLabel =
    isEpisode && item.unwatched_ahead_count > 0 ? `未看 ${item.unwatched_ahead_count} 集` : null;

  return (
    // 卡片是两个入口叠在一起：整卡进详情、中央播放键直接起播。播放键必须是
    // 卡片链接的**兄弟节点**——嵌在 <a> 里是无效 HTML，键盘与读屏都会错乱。
    // group/recent 因此上移到外壳（悬停播放键时整卡的抬升也要继续成立），
    // 焦点环仍挂在真正可聚焦的卡片链接上（group/card）。
    <div className="group/recent relative w-[224px] shrink-0 max-md:w-[200px] xl:w-[240px]">
      <Link
        href={itemHref(item)}
        aria-label={`${item.title}${context ? `，${context}` : ""}，${stateLabel(item, timeLabel != null)}${timeLabel ? `，已播 ${timeLabel}` : ""}${unwatchedLabel ? `，${unwatchedLabel}` : ""}`}
        {...tapGuard}
        className="group/card block outline-none"
      >
        <div
          className="relative aspect-video overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.38)] ring-1 ring-white/[0.08] transition duration-300 group-hover/recent:-translate-y-1 group-hover/recent:shadow-[0_18px_42px_rgba(0,0,0,0.55)] group-hover/recent:ring-white/25 group-focus-visible/card:ring-2 group-focus-visible/card:ring-white/80"
        >
          {artworkUrl ? (
            <PosterImage
              src={imageUrl(artworkUrl, "landscape-card")}
              alt={isEpisode ? `${item.title} ${code}分集剧照` : `${item.title}背景剧照`}
              className="size-full transition duration-500 group-hover/recent:scale-[1.03]"
              fallback={
                isEpisode ? (
                  <span className="tnum flex size-full items-center justify-center text-title-sm font-bold tracking-wide text-white/25">
                    {code}
                  </span>
                ) : (
                  <MoviePosterFill title={item.title} posterUrl={item.poster_url} />
                )
              }
            />
          ) : isEpisode ? (
            <span className="tnum flex size-full items-center justify-center text-title-sm font-bold tracking-wide text-white/25">
              {code}
            </span>
          ) : (
            <MoviePosterFill title={item.title} posterUrl={item.poster_url} />
          )}
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-black/75 to-transparent" />
          {/* 右上角统一放"状态角标"：看完的对勾与未看提示同排，不再和底部进度抢位置。 */}
          {(unwatchedLabel || item.played) && (
            <div className="pointer-events-none absolute right-2 top-2 flex items-center gap-1.5">
              {unwatchedLabel && (
                <span className="tnum rounded-full border border-emerald-200/25 bg-[rgba(5,46,34,0.76)] px-2 py-0.5 text-micro font-semibold text-emerald-100 shadow-[0_5px_16px_rgba(0,0,0,0.32)] backdrop-blur-md">
                  {unwatchedLabel}
                </span>
              )}
              {item.played && (
                <span className="flex size-6 items-center justify-center rounded-full bg-[var(--ok)] text-[#07120c] shadow-lg">
                  <CheckIcon className="size-3.5 stroke-[2.5]" />
                </span>
              )}
            </div>
          )}
          {/* 底部只留进度：看了一半时在进度条上方补一行「已播 / 总时长」。 */}
          <div className="pointer-events-none absolute inset-x-2 bottom-2 space-y-1">
            {timeLabel && (
              <span className="tnum text-on-image block text-micro font-semibold text-white/85">
                {timeLabel}
              </span>
            )}
            {progress != null ? (
              <div className="h-[3px] overflow-hidden rounded-full bg-white/25">
                <div
                  className={`h-full rounded-full ${item.played ? "bg-[var(--ok)]" : "bg-[var(--accent-2)]"}`}
                  style={{ width: `${progress}%` }}
                />
              </div>
            ) : (
              item.position_ms > 0 && (
                <div className="h-[3px] rounded-full bg-[var(--accent-2)]/60" />
              )
            )}
          </div>
        </div>
        <p className="mt-2 truncate text-ui font-semibold text-[var(--text)]">{item.title}</p>
        {context && (
          <p className="tnum mt-0.5 truncate text-sub text-[var(--text-muted)]">{context}</p>
        )}
        <p className="tnum mt-0.5 truncate text-caption text-[var(--text-faint)]">
          {stateLabel(item, timeLabel != null)}
        </p>
      </Link>

      {/* 中央播放键常驻，不做「悬停才出现」——它要说明的是「这张卡能直接看」，
          而触摸屏根本没有悬停态，藏起来等于对一半用户不存在。

          静息态是**空心**的：只有一圈描边和一个三角，圆内不填色、不加毛玻璃，
          剧照原样透出来（缩略图本身就小，中央糊掉一块等于把这张卡的主体挡了）。
          白描边压在浅色画面上会糊，所以靠外投影给它一圈暗晕、三角自带投影，
          两者都不遮挡画面就能拉出对比。悬停也**不翻成实心白**——那等于把
          「别挡封面」在用户凑近时收回；只把描边点亮到纯白、圆内浮一层
          15% 的白纱并轻微放大，反馈够清楚，封面始终看得见。

          外层 flex 用与剧照相同的 aspect-video 定位，保证圆心落在画面正中；
          抬升动画跟着卡片一起做，否则悬停时剧照上浮 4px、播放键留在原地。 */}
      <div className="pointer-events-none absolute inset-x-0 top-0 flex aspect-video items-center justify-center transition duration-300 group-hover/recent:-translate-y-1">
        <Link
          href={cardPlayHref(item)}
          aria-label={`${playVerb(item)}《${item.title}》${context ? ` ${context}` : ""}`}
          {...playTapGuard}
          className="pointer-events-auto flex size-11 items-center justify-center rounded-full border-[1.5px] border-white/75 text-white shadow-[0_1px_10px_rgba(0,0,0,0.45)] transition duration-200 hover:scale-[1.06] hover:border-white hover:bg-white/15 hover:shadow-[0_6px_20px_rgba(0,0,0,0.45)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 group-hover/recent:border-white max-md:size-10"
        >
          {/* 图标尺寸不等于三角尺寸：PlayIcon 的三角只占 24 视框的 47%×58%，
              按常规的 size-5 给，落到 44px 的圆里三角只有 8px，看着像个小点。
              这里按「三角高 ≈ 圆直径的 45%」倒推出 36px 的图标框。
              也不再额外右移——路径本身的重心就已经落在视框中心（起点 x=8、
              尖端 x=19.3，形心 ≈ 12），再推一次反而偏右。
              投影用来在任意画面上压住三角；悬停后按钮仍是通透的，投影不撤。 */}
          <PlayIcon className="size-9 drop-shadow-[0_1px_2px_rgba(0,0,0,0.55)] max-md:size-8" />
        </Link>
      </div>
    </div>
  );
}

/** 电影缺少横向剧照时：海报模糊铺底，中央按 2:3 完整保留一张清晰海报。 */
function MoviePosterFill({ title, posterUrl }: { title: string; posterUrl: string | null }) {
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
      <PosterImage
        src={src}
        alt=""
        className="absolute inset-0 size-full scale-125 blur-xl opacity-45"
      />
      <div className="absolute inset-0 bg-black/25" />
      <div className="absolute inset-y-0 left-1/2 w-[37.5%] -translate-x-1/2 shadow-[0_0_28px_rgba(0,0,0,0.55)]">
        <PosterImage src={src} alt={`${title}海报`} className="size-full" />
      </div>
    </div>
  );
}
