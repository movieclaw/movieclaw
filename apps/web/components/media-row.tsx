"use client";

import type { Route } from "next";
import Link from "next/link";

import { HScroller } from "@/components/h-scroller";
import { PosterCard, type PosterCardAction } from "@/components/poster-card";
import type { MediaItem, MediaRowData } from "@/lib/media-types";

/**
 * 横滚海报行（Netflix 式分类行）：标题栏 + 横滚容器（HScroller 提供隐藏
 * 滚动条与左右翻页钮）。所有行的卡片同一规格——榜单行不再有加宽的排名变体。
 */
export function MediaRow({
  row,
  moreHref,
  moreLabel = "查看完整榜单",
  cardAction,
  cardHref,
  cardRevealInfoOnTouch = false,
  insetClassName = "page-inset",
}: {
  row: MediaRowData;
  moreHref?: Route;
  moreLabel?: string;
  /**
   * 卡片悬浮操作区变体，缺省为「订阅影片」。媒体库「最近添加」行的动作
   * 因条目而异（在播剧追新/完结缺集补齐/齐全已入库），支持传函数逐条目决定。
   */
  cardAction?: PosterCardAction | ((item: MediaItem) => PosterCardAction);
  /**
   * 卡片点击目标覆盖：媒体库「最近添加」行跳库内条目详情（本地刮削信息），
   * 返回 undefined 的条目回落到默认的发现页详情。
   */
  cardHref?: (item: MediaItem) => Route | undefined;
  /**
   * 触摸端首次点按先展开卡片信息层。媒体库「最近添加」行没有悬浮操作键，
   * 默认规则不会给它「首点展开」，手机上季集范围与入库时间就完全读不到——
   * 这类纯信息层的行需要显式打开。
   */
  cardRevealInfoOnTouch?: boolean;
  /**
   * 行的左右留白，缺省 page-inset（主题变量档：银玻璃 px-6、Netflix 4vw）。
   * 影片详情页正文用 content-inset（px-12 档），行不跟着改的话标题与海报
   * 会比上方的简介、剧照墙往左戳出一截。
   */
  insetClassName?: string;
}) {
  return (
    // content-visibility：视口外的整行（标题 + 几十张海报卡）跳过布局与绘制，
    // 发现页 16 行 / 库首页多行「最近添加」的首帧与滚动成本只与可见行相关；
    // intrinsic-size 先占住一行的近似高度（auto 记住实测值），滚动条不跳
    <section className="relative [contain-intrinsic-size:auto_330px] [content-visibility:auto]">
      <div
        className={`mb-3 flex items-center justify-between gap-4 max-md:mb-2 ${insetClassName}`}
      >
        <h3 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          {row.title}
        </h3>
        {moreHref && (
          <Link
            href={moreHref}
            className="shrink-0 text-sub font-semibold text-[var(--text-muted)] transition hover:text-[var(--text)]"
          >
            {moreLabel}
          </Link>
        )}
      </div>

      {/* m-row：稳定钩子类，供 Netflix 主题的移动端行卡宽断点规则定位
          （globals.css 的 html[data-theme="netflix"] .m-row > div）；
          银玻璃不命中该作用域，卡宽维持下方工具类取值 */}
      <HScroller className={`m-row gap-4 pb-1 pt-1 max-md:gap-3 ${insetClassName}`}>
        {row.items.map((item) => (
          <div
            key={`${row.id}-${item.id}`}
            className="w-[152px] shrink-0 max-md:w-[126px] xl:w-[164px]"
          >
            <PosterCard
              item={item}
              action={typeof cardAction === "function" ? cardAction(item) : cardAction}
              href={cardHref?.(item)}
              revealInfoOnTouch={cardRevealInfoOnTouch}
            />
          </div>
        ))}
      </HScroller>
    </section>
  );
}
