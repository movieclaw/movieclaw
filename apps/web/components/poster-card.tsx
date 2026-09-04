"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";

import type { Route } from "next";
import Link from "next/link";

import { BellIcon, CheckIcon, DownloadIcon, PlusIcon, StarIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { useMediaDetail } from "@/lib/media-detail";
import type {
  MediaItem,
  MediaLibraryStatus,
  MediaOverlayDetails,
  MediaType,
} from "@/lib/media-types";
import { useMediaQuery } from "@/lib/use-media-query";
import { useTapGuard } from "@/lib/use-tap-guard";

/**
 * 海报卡片：发现页海报墙的最小单元（Netflix 式）。
 *
 * 结构分两层：
 *   - 海报区：2:3 竖版海报，常显「评分徽章（右上）+ 最高清晰度徽章（左上）」；
 *     hover 时整卡上浮、海报放大，底部升起渐变信息层（类型 / 简介 / 订阅影片按钮）。
 *   - 文字区：海报下方常显标题与「年份 · 规模」，保证不 hover 也能扫读海报墙。
 *
 * 全站海报卡只有这一种形态：曾经给榜单行做过「左侧描边大数字」的排名变体，
 * 但发现页里排名行有好几条（豆瓣实时热门/口碑榜/Top 250 …），满屏大数字过于
 * 抢眼、也把行与行的节奏打乱，已整体去掉——榜单的名次由行标题表达即可。
 */
export interface PosterCardProps {
  item: MediaItem;
  /** 悬浮层的操作区变体，默认「订阅影片」 */
  action?: PosterCardAction;
  /** 点击目标覆盖：传入即直接链接到该地址（媒体库「最近添加」行跳
   *  库内条目详情），缺省走发现页详情（useMediaDetail.open） */
  href?: Route;
  /** 触摸端首次点按是否先展开纯信息层；默认仍单击直达详情。 */
  revealInfoOnTouch?: boolean;
}

/**
 * 悬浮层操作区的形态，按内容与用户的关系选择：
 * - subscribe：还没拥有（发现页/搜索），给「订阅影片」入口；
 * - follow：当前已知季集全部在库，给「自动续订」入口；
 * - backfill：当前已知季集尚未收齐，给「补齐缺集」入口
 *   （订阅创建按 E−H 跳过库里已有的集，只为缺的集生成工单）；
 * - owned：已在媒体库且无后续动作（电影/完结齐全剧），静态「在库」标识；
 * - none：已订阅但尚未落地（单库页「追踪中」），再给订阅按钮是重复操作，不显示。
 *
 * 前三种都是订阅入口，仅文案/图标不同；该影片已存在订阅时完全隐藏操作，
 * 状态来自 SubscribeEntryProvider 的全站订阅列表，卡片自身不发请求。
 */
export type PosterCardAction = "subscribe" | "follow" | "backfill" | "owned" | "none";

/** 三种订阅入口的文案与图标（已订阅时统一切换为状态徽标，不走这张表）。
 *  订阅是「加入追踪清单」，图标用 +（加入）与 🔔（追新）表意——
 *  不能用播放三角：它承诺的是「点了就能看」，会误导用户以为是播放键。 */
const SUBSCRIBE_ACTION_META = {
  subscribe: { label: "订阅影片", Icon: PlusIcon },
  follow: { label: "自动续订", Icon: BellIcon },
  backfill: { label: "补齐缺集", Icon: DownloadIcon },
} as const;

/**
 * 海报卡片的最小视觉契约。搜索结果不含年份和类型，缺失字段保持不显示，
 * 但仍复用与发现页完全相同的海报、评分、悬浮信息层和来源标识。
 */
export interface PosterVisualItem {
  /** Discover 返回的稳定条目引用；列表数据有值，历史/本地条目可能没有。 */
  titleRef?: string;
  id: string;
  title: string;
  rating: number;
  posterUrl: string;
  source?: "tmdb" | "douban";
  /** 电影/剧集；豆瓣轻量搜索结果没有该字段，订阅入口点击时补拉详情识别 */
  type?: MediaType;
  year?: number;
  genres?: string[];
  extent?: string;
  badges?: string[];
  /** 海报角上的斜向常显标签（如「自动续订」「已订阅」）；
   *  「已入库」不必显式传，由 libraryStatus 自动派生（见 resolveRibbon）。 */
  ribbon?: string;
  /** 人物作品页使用更小的左上角斜标；缺省保持订阅墙现有样式。 */
  ribbonVariant?: "compact-left";
  /** 斜标的语义色：已入库为绿色，已订阅为蓝色，未识别为琥珀色。 */
  ribbonTone?: "owned" | "subscribed" | "warning";
  /** 主图宽高比；缺省 2:3 海报框。本地抓帧的缩略图是 16:9，硬塞进海报框会裁掉两边 */
  aspect?: number;
  /** 海报底部常显的一行左右信息；订阅墙用来承载剧集范围与收录进度。
   *  tracking=追更中绿点；upgrading=洗版中青点（绿点优先，同槽位只亮一个）；
   *  upgradingCount=青点旁的「洗 N」数量——只有点没有字用户感知不到洗版仍在进行。 */
  posterFooter?: {
    label: string;
    value: string;
    tracking?: boolean;
    upgrading?: boolean;
    upgradingCount?: number;
  };
  /** 悬浮层的一行紧凑元信息；长内容截断，完整值保留在 title 中。 */
  overlayMeta?: string;
  /** 悬浮层的两行紧凑上下文；不占用海报下方的常显元信息。 */
  overlayDetails?: MediaOverlayDetails;
  overview?: string;
  libraryStatus?: MediaLibraryStatus | null;
}

/**
 * 海报角标的最终形态（无角标返回 null）。
 *
 * 「已入库」曾有两套表达：人物作品页在海报左上角打绿色斜标，其余发现页只在
 * 海报下方的年份后面缀一枚绿点。绿点小且混在灰色元信息里，扫墙时几乎看不见，
 * 现全站统一到斜标——凡是带 libraryStatus 的海报都自动打「已入库」绿斜标。
 * 调用方显式传 ribbon（订阅墙的「自动续订」、人物作品页的「已订阅」）时以调用方为准。
 */
function resolveRibbon(
  item: PosterVisualItem,
): { label: string; compactLeft: boolean; tone: "owned" | "subscribed" | "warning" } | null {
  if (item.ribbon) {
    return {
      label: item.ribbon,
      compactLeft: item.ribbonVariant === "compact-left",
      tone: item.ribbonTone ?? "subscribed",
    };
  }
  if (item.libraryStatus) return { label: "已入库", compactLeft: true, tone: "owned" };
  return null;
}

export function PosterCard({ item, action, href, revealInfoOnTouch }: PosterCardProps) {
  // 点击整卡（含 hover 信息层）进入该影片的详情页
  const { open } = useMediaDetail();
  if (href) {
    return (
      <PosterCardVisual
        item={item}
        action={action}
        href={href}
        revealInfoOnTouch={revealInfoOnTouch}
      />
    );
  }
  return (
    <PosterCardVisual
      item={item}
      action={action}
      onClick={() => open(item)}
      revealInfoOnTouch={revealInfoOnTouch}
    />
  );
}

/** 统一海报视觉组件；传入 onClick 时才渲染为可点击按钮。 */
export function PosterCardVisual({
  item,
  onClick,
  href,
  action = "subscribe",
  revealInfoOnTouch = false,
}: {
  item: PosterVisualItem;
  onClick?: () => void;
  href?: Route;
  action?: PosterCardAction;
  /** 触摸端首次点按是否先展开纯信息层；默认仍单击直达详情。 */
  revealInfoOnTouch?: boolean;
}) {
  const { canSubscribe, subscriptionOf } = useSubscribeEntry();
  const hasSubscribeAction =
    action === "subscribe" || action === "follow" || action === "backfill";
  // 媒体库的补齐/续订按钮在条目进入“我的订阅”后完全隐藏；发现页原本的
  // “订阅影片 → 已订阅”状态反馈仍保留，不能被这条库存规则误伤。
  const hideSubscribedLibraryAction =
    (action === "follow" || action === "backfill") && Boolean(subscriptionOf(item));
  const showOverlayAction =
    action === "owned" || (hasSubscribeAction && canSubscribe && !hideSubscribedLibraryAction);
  const showOverlay = Boolean(
    item.overlayDetails?.primary.trim() ||
      item.genres?.length ||
      item.overlayMeta?.trim() ||
      item.overview?.trim() ||
      showOverlayAction,
  );
  // 默认只有可点击的次级操作需要「首次展开」；订阅墙可显式要求纯信息层
  // 也先展开，让没有 hover 的手机仍能读到选季与配置流向。
  const revealOnTouch =
    (hasSubscribeAction && canSubscribe) || (revealInfoOnTouch && showOverlay);
  // 触摸端没有 hover：需要展示信息层时，第一次点按只「展开」（等价于
  // 鼠标悬停），看清后再点海报才进详情；无需展开的卡片仍单击直达详情。
  // 点卡片外任意处收起。之前的方案是在海报角上常驻一枚订阅圆键，但图标
  // 无文案表意不清，且张张海报都印着按钮、墙面很吵。
  const noHover = useMediaQuery("(hover: none)");
  const [revealed, setRevealed] = useState(false);
  const rootRef = useRef<HTMLElement | null>(null);

  // 展开后点卡片外任意处收起（pointerdown 而非 click：滚动/拖动开始就该收）
  useEffect(() => {
    if (!revealed) return;
    const onDown = (e: globalThis.PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setRevealed(false);
    };
    document.addEventListener("pointerdown", onDown, { capture: true });
    return () => document.removeEventListener("pointerdown", onDown, { capture: true });
  }, [revealed]);

  // 触摸端误触保护：海报墙滑动远多于点击，浏览器原生的 click 抑制拦不住
  // 「滑到边缘」「点一下停惯性」这类手势，统一交给 useTapGuard 判定。
  // Link 分支不传动作，仅靠 preventDefault 拦掉不合格点击的跳转。
  const tapGuard = useTapGuard(
    onClick &&
      (() => {
        if (noHover && revealOnTouch && !revealed) {
          setRevealed(true);
          return;
        }
        onClick();
      }),
  );
  // Link 分支的「首点展开」：误触判定放行、且信息层未展开时，拦下跳转改为展开
  const onLinkClick = (e: MouseEvent) => {
    tapGuard.onClick(e);
    if (e.defaultPrevented) return;
    if (noHover && revealOnTouch && !revealed) {
      e.preventDefault();
      setRevealed(true);
    }
  };
  const content = (
    <PosterCardContent
      item={item}
      action={action}
      showOverlay={showOverlay}
      showOverlayAction={showOverlayAction}
    />
  );
  const interactiveClass =
    "group/card block w-full cursor-pointer rounded-2xl text-left outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]";
  if (href) {
    const accessibilityDetails = [
      resolveRibbon(item)?.label,
      item.posterFooter
        ? `${item.posterFooter.label}，收录 ${item.posterFooter.value}` +
          (item.posterFooter.upgrading
            ? item.posterFooter.upgradingCount
              ? `，${item.posterFooter.upgradingCount} 集洗版中`
              : "，洗版中"
            : "")
        : undefined,
      item.overlayDetails?.primary,
      item.overlayDetails?.secondary,
      item.genres?.join("、"),
      item.overlayMeta,
    ].filter(Boolean);
    return (
      <Link
        href={href}
        scroll={false}
        ref={rootRef as React.Ref<HTMLAnchorElement>}
        {...tapGuard}
        onClick={onLinkClick}
        data-revealed={revealed}
        className={interactiveClass}
        aria-label={`查看《${item.title}》详情${accessibilityDetails.length > 0 ? `，${accessibilityDetails.join("，")}` : ""}`}
      >
        {content}
      </Link>
    );
  }
  if (!onClick) return <div className="group/card block w-full text-left">{content}</div>;
  return (
    <button
      type="button"
      ref={rootRef as React.Ref<HTMLButtonElement>}
      {...tapGuard}
      data-revealed={revealed}
      className={interactiveClass}
    >
      {content}
    </button>
  );
}

function PosterCardContent({
  item,
  action = "subscribe",
  showOverlay,
  showOverlayAction,
}: {
  item: PosterVisualItem;
  action?: PosterCardAction;
  showOverlay: boolean;
  showOverlayAction: boolean;
}) {
  const badges = item.badges ?? [];
  const genres = item.genres ?? [];
  const overlayPrimary = item.overlayDetails?.primary.trim() ?? "";
  const overlaySecondary = item.overlayDetails?.secondary?.trim() ?? "";
  const overlayMeta = item.overlayMeta?.trim() ?? "";
  const overview = item.overview ?? "";
  const ribbon = resolveRibbon(item);
  const compactRibbonTone =
    ribbon?.tone === "owned"
      ? "from-emerald-500 via-green-500 to-teal-500 shadow-[0_2px_8px_rgba(16,185,129,0.38)]"
      : ribbon?.tone === "warning"
        ? "from-amber-500 via-orange-500 to-amber-600 shadow-[0_2px_8px_rgba(245,158,11,0.38)]"
        : "from-sky-500 via-blue-500 to-indigo-500 shadow-[0_2px_8px_rgba(59,130,246,0.38)]";
  return (
    <>
      {/* 海报区（自身 relative：徽章与 hover 信息层都绝对定位在它内部） */}
      <div
        // 比例走内联样式：其他库的抓帧缩略图是 16:9，TMDB 海报是 2:3，同一张墙上按条目各自排版
        style={{ aspectRatio: item.aspect ?? 2 / 3 }}
        className="relative w-full overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.4)] ring-1 ring-white/[0.08] transition-all duration-300 ease-out group-hover/card:-translate-y-1.5 group-hover/card:shadow-[0_22px_50px_rgba(0,0,0,0.6)] group-hover/card:ring-white/25"
      >
        <PosterImage
          src={item.posterUrl}
          alt={`${item.title} 海报`}
          className="absolute inset-0 size-full transition-transform duration-500 ease-out group-hover/card:scale-[1.06]"
        />

        {/* 左上：资源最高清晰度徽章（无资源信息时不渲染）。
            徽章不用 backdrop-blur：海报墙每张卡 2~3 个模糊合成层会显著放大
            滚动时的 GPU 压力（几百张卡叠加），加实底色观感几乎无差 */}
        {badges[0] && (
          <span className="absolute left-2 top-2 rounded-md bg-black/70 px-1.5 py-0.5 text-micro font-bold tracking-wide text-[var(--accent)]">
            {badges[0]}
          </span>
        )}
        {/* 作品状态用紧凑左上斜标，宽度随海报缩放且不遮评分；其余调用方
            沿用右侧规格。无状态不渲染，避免海报墙过吵。 */}
        {ribbon && (
          <span
            aria-label={ribbon.label}
            className={
              ribbon.compactLeft
                ? `pointer-events-none absolute -left-[18%] top-2 z-10 w-[62%] -rotate-45 border-y border-white/20 bg-gradient-to-r py-0.5 text-center text-[9px] font-bold leading-[11px] tracking-[0.08em] text-white ${compactRibbonTone}`
                : "pointer-events-none absolute -right-8 top-4 z-10 w-28 rotate-45 border-y border-white/25 bg-gradient-to-r from-cyan-500 via-sky-500 to-violet-500 py-1 text-center text-[10px] font-bold tracking-[0.12em] text-white shadow-[0_3px_14px_rgba(14,165,233,0.45)]"
            }
          >
            {ribbon.label}
          </span>
        )}
        {/* 右上：洗版徽标（订阅墙专属，posterFooter 才会带 upgradingCount；
            订阅墙不渲染评分徽章，右上角位无冲突）。青色与详情页洗版色同源 */}
        {item.posterFooter?.upgrading && item.posterFooter.upgradingCount != null && (
          // 淡暗底把徽标从海报里衬出来（全透明会融进画面），浓度压到 40%
          // 保持轻盈；不用 backdrop-blur（海报墙的 GPU 成本约束见上方注释）
          <span className="tnum absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md bg-black/40 px-1.5 py-0.5 text-caption font-semibold text-[#2dd4bf] [text-shadow:0_1px_2px_rgba(0,0,0,0.6)]">
            <span
              aria-hidden="true"
              className="size-1.5 rounded-full bg-[#2dd4bf] shadow-[0_0_6px_rgba(45,212,191,0.7)]"
            />
            洗版 {item.posterFooter.upgradingCount}
          </span>
        )}
        {/* 右上：评分徽章（暂无评分时不渲染，避免展示 0.0） */}
        {item.rating > 0 && (
          <span
            className={`tnum absolute right-2 flex items-center gap-1 rounded-md bg-black/70 px-1.5 py-0.5 text-caption font-semibold text-white ${ribbon && !ribbon.compactLeft ? "top-12" : "top-2"}`}
          >
            <StarIcon className="size-3 text-[var(--warn)]" />
            {item.rating.toFixed(1)}
          </span>
        )}

        {/* 订阅墙的剧集收录摘要常驻海报内部，利用底部暗部承载一眼可扫的
            “范围 / 数量”；悬浮信息层出现时让位，避免两层文字叠在一起。 */}
        {item.posterFooter && (
          <div
            className={`pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/55 to-transparent px-3 pb-2.5 pt-9 ${showOverlay ? "transition-opacity duration-200 group-hover/card:opacity-0 group-focus-visible/card:opacity-0 group-data-[revealed=true]/card:opacity-0" : ""}`}
          >
            <p className="flex items-center justify-between gap-2 text-caption text-white/80">
              <span className="truncate">{item.posterFooter.label}</span>
              <span className="tnum flex shrink-0 items-center gap-1.5 font-semibold text-white">
                {item.posterFooter.tracking ? (
                  <span
                    aria-hidden="true"
                    className="size-1.5 rounded-full bg-[var(--ok)] shadow-[0_0_7px_rgba(74,222,128,0.55)]"
                  />
                ) : item.posterFooter.upgrading ? (
                  <span
                    aria-hidden="true"
                    className="size-1.5 rounded-full bg-[#2dd4bf] shadow-[0_0_7px_rgba(45,212,191,0.55)]"
                  />
                ) : null}
                {item.posterFooter.value}
              </span>
            </p>
          </div>
        )}

        {/* hover 信息层：底部渐变升起，展示类型 / 简介 / 快捷操作。有可点击的
            次级操作时，触摸端由首次点按触发（根节点的 data-revealed）；纯信息层
            不拦截主导航——手机单击直接进入详情。 */}
        {showOverlay && (
          <div className="absolute inset-x-0 bottom-0 translate-y-3 bg-gradient-to-t from-black/90 via-black/60 to-transparent px-3 pb-3 pt-10 opacity-0 transition-all duration-300 ease-out group-hover/card:translate-y-0 group-hover/card:opacity-100 group-focus-visible/card:translate-y-0 group-focus-visible/card:opacity-100 group-data-[revealed=true]/card:translate-y-0 group-data-[revealed=true]/card:opacity-100">
            {overlayPrimary && (
              <div>
                <p className="text-caption font-semibold text-[var(--accent-2)]">
                  {overlayPrimary}
                </p>
                {overlaySecondary && (
                  <p className="mt-1 text-caption leading-4 text-white/75">
                    {overlaySecondary}
                  </p>
                )}
              </div>
            )}
            {genres.length > 0 && (
              <p
                className={`${overlayPrimary ? "mt-1" : ""} text-caption font-medium text-[var(--accent-2)]`}
              >
                {genres.join(" · ")}
              </p>
            )}
            {overlayMeta && (
              <p
                title={overlayMeta}
                className={`${overlayPrimary || genres.length > 0 ? "mt-1" : ""} truncate text-caption leading-4 text-white/75`}
              >
                {overlayMeta}
              </p>
            )}
            {overview && (
              <p className="mt-1 line-clamp-3 text-caption leading-4 text-white/75">
                {overview}
              </p>
            )}
            {showOverlayAction && (
              <div className="mt-2.5 flex items-center gap-2">
                <PosterCardActionButton item={item} action={action} />
              </div>
            )}
          </div>
        )}
      </div>

      {/* 文字区：常显标题 + 元信息（压在背景大图上，需 text-on-image 投影保证可读） */}
      <div className="mt-2">
        <p className="text-on-image truncate text-ui font-semibold text-[var(--text)]">
          {item.title}
        </p>
        {/* 始终保留一行元信息，避免缺失年份让同一海报墙的卡片高低跳动。
            在库与否不在这里表达——它已由海报左上角的「已入库」斜标承担。 */}
        <p className="text-on-image tnum mt-0.5 flex min-h-4 items-center gap-1.5 truncate text-caption text-[var(--text-muted)]">
          {!!item.year && <span>{item.year}</span>}
          {!!item.year && item.extent && <span>·</span>}
          {item.extent && <span>{item.extent}</span>}
        </p>
      </div>
    </>
  );
}

/**
 * 信息层里的操作键（订阅 / 追新 / 补齐 / 在库标识）。
 *
 * 外层整卡已经是 button / Link，内部不能再嵌 button（HTML 不允许交互元素嵌套），
 * 所以用 role="button" 的 span 承载：preventDefault 拦掉 Link 跳转，
 * stopPropagation 拦掉整卡 onClick，点它就只做订阅这一件事。
 */
function PosterCardActionButton({
  item,
  action,
}: {
  item: PosterVisualItem;
  action: PosterCardAction;
}) {
  const { canSubscribe, open: openSubscribe, subscriptionOf } = useSubscribeEntry();
  const subscribeMeta =
    action === "subscribe" || action === "follow" || action === "backfill"
      ? SUBSCRIBE_ACTION_META[action]
      : null;
  const existingSub = subscribeMeta ? subscriptionOf(item) : undefined;
  const trigger = () => void openSubscribe(item);
  // 圆键就贴在海报角上，横滑时拇指最容易蹭到，同样过一遍误触判定。
  // Hook 不能放在下面的提前返回之后，故与 trigger 一起提到最前。
  const tapGuard = useTapGuard(trigger);

  if (!subscribeMeta) {
    // 在库标识：非交互，绿点沿用全站「在库」的语义色（var(--ok)）。
    return (
      <span className="flex h-7 items-center gap-1.5 rounded-full bg-white/[0.18] px-3 text-caption font-semibold text-white/90">
        <span className="size-1.5 rounded-full bg-[var(--ok)]" />
        在库
      </span>
    );
  }
  // 媒体库动作已有订阅时由外层直接移除；这里再守一道，避免未来新的调用点
  // 绕过 showOverlayAction。发现页 subscribe 仍显示“已订阅”反馈。
  if (!canSubscribe || (existingSub && action !== "subscribe")) return null;

  const label = existingSub
    ? `管理《${item.title}》的订阅`
    : `${subscribeMeta.label}《${item.title}》`;

  return (
    <span
      role="button"
      tabIndex={0}
      aria-label={label}
      onPointerDown={tapGuard.onPointerDown}
      onPointerUp={tapGuard.onPointerUp}
      onPointerCancel={tapGuard.onPointerCancel}
      onClick={(e) => {
        // 无论判定结果如何都先拦住冒泡：这颗键嵌在整卡的 button/Link 里，
        // 放行会让「订阅」顺带把详情页也打开。判定通过与否交给 tapGuard。
        e.preventDefault();
        e.stopPropagation();
        tapGuard.onClick(e);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          trigger();
        }
      }}
      className={
        existingSub
          ? "flex h-7 items-center gap-1.5 rounded-full bg-white/[0.18] px-3 text-caption font-semibold text-white/90 transition-colors hover:bg-white/[0.26]"
          : "btn-accent flex h-7 items-center gap-1 rounded-full px-3 text-caption font-semibold"
      }
    >
      {existingSub ? (
        <>
          <CheckIcon className="size-3 text-[var(--ok)]" />
          已订阅
        </>
      ) : (
        <>
          <subscribeMeta.Icon className="size-3" />
          {subscribeMeta.label}
        </>
      )}
    </span>
  );
}
