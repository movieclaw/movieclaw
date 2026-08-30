"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import {
  CalendarIcon,
  ChevronRightIcon,
  ClockIcon,
  CompassIcon,
} from "@/components/icons";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import type { DownloadTask } from "@/lib/api/downloaders";
import {
  checkSubscriptionAutomationReadiness,
  listRuleSets,
  listTodaySubscriptionArrivals,
  type RuleSet,
  type Subscription,
  type TodaySubscriptionArrival,
} from "@/lib/api/subscriptions";
import { useDownloadTasks } from "@/lib/download-tasks";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import { cachedImageUrl } from "@/lib/image-proxy";
import { usePageChrome } from "@/lib/page-chrome";
import { usePermissions } from "@/lib/permissions";
import {
  nextSubscriptionVisibleCount,
  SUBSCRIPTION_BATCH_SIZE,
  subscriptionOverviewSections,
  type SubscriptionFilter,
  type SubscriptionKind,
} from "@/lib/subscription-overview";
import {
  groupTodayArrivals,
  subscriptionCollectionMeta,
  subscriptionFullyCollected,
  subscriptionRibbon,
  todayArrivalPresentation,
  type TodayArrivalPresentation,
} from "@/lib/subscription-ui";
import { useIsMobile } from "@/lib/use-media-query";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";
import { useVisiblePolling } from "@/lib/use-visible-polling";

const TODAY_ARRIVALS_POLL_MS = 10_000;

const rememberedVisibleCounts: Record<SubscriptionKind, number> = {
  movie: SUBSCRIPTION_BATCH_SIZE,
  tv: SUBSCRIPTION_BATCH_SIZE,
};

/**
 * 今日时间轨道的状态色：只承担进度语义，不与下方海报墙争夺视觉焦点。
 *
 * 颜色按「事情有没有真的发生」升级：预计入库还没开始，走中性灰不发光；
 * 到点没拿到资源转 --warn，真正在下载转 --info，收尾转 --ok。这样一屏里
 * 亮起来的点就是此刻真有进展的那几行，而不是满屏紫点。
 */
const todayArrivalStyle: Record<
  TodayArrivalPresentation["statusLabel"],
  { node: string; status: string; time: string; pulse: boolean }
> = {
  预计入库: {
    // 这是最常见的一行，节点压到刚好看得见即可——否则「什么都还没发生」
    // 会成为整屏最亮的东西，把真正有进展的几行盖过去。
    node: "border-white/35 bg-white/55",
    status: "border-white/[0.12] bg-white/[0.06] text-white/70",
    time: "text-white/82",
    pulse: false,
  },
  等待资源: {
    node: "border-[var(--warn)]/70 bg-[var(--warn)] shadow-[0_0_12px_rgba(245,196,81,0.32)]",
    status: "border-[var(--warn)]/20 bg-[var(--warn)]/10 text-[var(--warn)]",
    time: "text-[var(--warn)]",
    pulse: false,
  },
  下载中: {
    node: "border-[var(--info)]/75 bg-[var(--info)] shadow-[0_0_14px_rgba(127,176,255,0.42)]",
    status: "border-[var(--info)]/25 bg-[var(--info)]/12 text-[var(--info)]",
    time: "text-[var(--info)]",
    pulse: true,
  },
  整理中: {
    node: "border-[var(--ok)]/70 bg-[var(--ok)] shadow-[0_0_13px_rgba(74,222,128,0.38)]",
    status: "border-[var(--ok)]/20 bg-[var(--ok)]/10 text-[var(--ok)]",
    time: "text-[var(--ok)]",
    pulse: false,
  },
};

/**
 * 订阅页：用户全部订阅的海报墙。
 *
 * 数据直接消费 SubscribeEntryProvider 的全站订阅列表（唯一数据源）：
 * 弹层里取消订阅后 context 刷新，这里的墙面即时同步，无需各自维护快照。
 * 进入本页时主动 refresh 一次，保证订阅清单是新鲜的。
 *
 * 结构：页头（标题 + 二级导航）+ 今日摘要 + 剧集/电影分区海报墙。
 * 海报常显自动续订与真实收录摘要；桌面悬浮时补充选季与
 * 「规则 → 媒体库」，瞬时执行状态统一去任务中心查看。
 */
export function SubscriptionsView() {
  const { canManageSubscriptions, canSubscribe, isAdmin } = usePermissions();
  const restoreScrollRef = useScrollRestoration("subscriptions", {
    anchorAttribute: "data-subscription-id",
  });
  const scrollRootRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useCallback(
    (node: HTMLDivElement | null) => {
      scrollRootRef.current = node;
      restoreScrollRef(node);
    },
    [restoreScrollRef],
  );
  const [mediaType, setMediaType] = useState<SubscriptionFilter>("all");
  const { subscriptions, refresh } = useSubscribeEntry();
  const { tasks: downloadTasks } = useDownloadTasks();
  const [failed, setFailed] = useState(false);
  const [todayArrivals, setTodayArrivals] = useState<TodaySubscriptionArrival[] | null>(null);
  const [todayArrivalsFailed, setTodayArrivalsFailed] = useState(false);
  const todayArrivalsRequestRef = useRef(0);
  const hasTodayArrivalsSnapshotRef = useRef(false);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  // 链路体检整体为 error 时顶部亮警示横幅（提醒推到用户在的地方，
  // 全景与修复入口在 设置 → 概览）。拉取失败静默——横幅只是提示层
  const [healthIssue, setHealthIssue] = useState<{ libraryErrors: number } | null>(null);

  const reload = useCallback(() => {
    setFailed(false);
    void refresh().then((ok) => setFailed(!ok));
    if (canManageSubscriptions) {
      void checkSubscriptionAutomationReadiness()
        .then((h) =>
          setHealthIssue(h.status === "error" ? { libraryErrors: h.error_count } : null),
        )
        .catch(() => setHealthIssue(null));
    } else {
      setHealthIssue(null);
    }
  }, [canManageSubscriptions, refresh]);

  useEffect(() => {
    reload();
  }, [reload]);

  // 只要有订阅就取预告：电影在下载/整理阶段同样进这块，切到电影分区也要有内容。
  const todayArrivalsEnabled = subscriptions !== null && subscriptions.length > 0;
  const refreshTodayArrivals = useCallback(() => {
    if (!todayArrivalsEnabled) return;
    const requestId = ++todayArrivalsRequestRef.current;
    void listTodaySubscriptionArrivals()
      .then((rows) => {
        if (requestId !== todayArrivalsRequestRef.current) return;
        hasTodayArrivalsSnapshotRef.current = true;
        setTodayArrivals(rows);
        setTodayArrivalsFailed(false);
      })
      .catch(() => {
        if (
          requestId === todayArrivalsRequestRef.current &&
          !hasTodayArrivalsSnapshotRef.current
        ) {
          setTodayArrivals([]);
          setTodayArrivalsFailed(true);
        }
      });
  }, [todayArrivalsEnabled]);

  // 首次进入与订阅清单变化时立即读取，停留期间每 10 秒静默同步；
  // 后台标签页暂停，恢复可见时补一次。已有快照
  // 遇到瞬时请求失败继续保留，避免时间轨道闪成错误态或重新出现加载文案。
  useEffect(() => {
    if (!todayArrivalsEnabled) {
      todayArrivalsRequestRef.current += 1;
      hasTodayArrivalsSnapshotRef.current = false;
      setTodayArrivals(null);
      setTodayArrivalsFailed(false);
      return;
    }
    setTodayArrivalsFailed(false);
    refreshTodayArrivals();
  }, [refreshTodayArrivals, subscriptions, todayArrivalsEnabled]);
  useVisiblePolling(
    refreshTodayArrivals,
    todayArrivalsEnabled ? TODAY_ARRIVALS_POLL_MS : null,
  );

  // 配置名称不是订阅摘要的一部分，分别用现有列表接口补齐。规则组沿用详情页
  // 的管理员可见边界；媒体库接口会按成员白名单过滤，因此所有登录用户都可安全读取。
  useEffect(() => {
    let cancelled = false;
    void listLibraries()
      .then((rows) => {
        if (!cancelled) setLibraries(rows);
      })
      .catch(() => {
        if (!cancelled) setLibraries([]);
      });
    if (canManageSubscriptions) {
      void listRuleSets()
        .then((rows) => {
          if (!cancelled) setRuleSets(rows);
        })
        .catch(() => {
          if (!cancelled) setRuleSets([]);
        });
    } else {
      setRuleSets([]);
    }
    return () => {
      cancelled = true;
    };
  }, [canManageSubscriptions]);

  // 全部/剧集/电影切换在移动端挂进全局顶栏右上角（字标与搜索之间那段空位），
  // 与发现页的数据源切换同一套安放方式——窄屏页头不再为它单独堆一行。
  // 桌面端则作为标题下方的二级导航，避免把内容分类误读成页面操作。
  const chrome = usePageChrome();
  const isMobile = useIsMobile();
  const setTopBarActions = chrome?.setTopBarActions;
  const switchMediaType = useCallback(
    (next: SubscriptionFilter) => {
      if (next === mediaType) return;
      scrollRootRef.current?.scrollTo({ top: 0 });
      setMediaType(next);
    },
    [mediaType],
  );
  useEffect(() => {
    if (!isMobile || !setTopBarActions) return;
    return setTopBarActions(
      <MediaTypeSwitcher compact value={mediaType} onChange={switchMediaType} />,
    );
  }, [isMobile, mediaType, setTopBarActions, switchMediaType]);

  const allSections = useMemo(
    () => subscriptionOverviewSections(subscriptions ?? [], "all"),
    [subscriptions],
  );
  const tvSubscriptions =
    allSections.find((section) => section.kind === "tv")?.subscriptions ?? [];
  const movieSubscriptions =
    allSections.find((section) => section.kind === "movie")?.subscriptions ?? [];
  const visible =
    mediaType === "all"
      ? subscriptions ?? []
      : mediaType === "tv"
        ? tvSubscriptions
        : movieSubscriptions;
  const ruleSetNameById = useMemo(
    () => new Map(ruleSets.map((rule) => [rule.id, rule.name])),
    [ruleSets],
  );
  const libraryNameById = useMemo(
    () => new Map(libraries.map((library) => [library.id, library.name])),
    [libraries],
  );
  const defaultLibraryNameByKind = useMemo(
    () =>
      new Map(
        libraries
          .filter((library) => library.is_default)
          .map((library) => [library.kind, library.name]),
      ),
    [libraries],
  );

  return (
    <div
      ref={scrollRef}
      data-scroll-root
      className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10"
    >
      <div className="px-6 pt-7 max-md:px-4 max-md:pt-4">
        <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
          我的订阅
        </h2>
        <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:text-sub">
          {mediaType === "all"
            ? `共 ${visible.length} 部订阅 · ${tvSubscriptions.length} 部剧集 · ${movieSubscriptions.length} 部电影`
            : `共 ${visible.length} 部${mediaType === "movie" ? "电影" : "剧集"}`}
          {" · "}movieclaw 会持续追踪并在新资源放出后自动入库
        </p>
        {!isMobile && (
          <div className="mt-4">
            <MediaTypeSwitcher value={mediaType} onChange={switchMediaType} />
          </div>
        )}
      </div>

      {/* 链路警示横幅：只在体检整体为 error 时出现——订阅不会丢（工单退避
          重试），但在修好之前无法自动下载入库。点击进设置概览看体检全景与修复 */}
      {canManageSubscriptions && healthIssue && (
        <Link
          href={"/settings/overview" as Route}
          className="mx-6 mt-4 block rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-relaxed text-amber-200 transition hover:bg-amber-500/15 max-md:mx-4"
        >
          {healthIssue.libraryErrors > 0
            ? `${healthIssue.libraryErrors} 个媒体库的入库链路有问题，相关订阅暂时无法自动下载入库（已下达的任务会自动重试）`
            : "订阅链路尚未就绪（缺少可用的资源站点或下载器），订阅暂时只能记录意愿"}
          ——点击查看体检详情与修复入口 →
        </Link>
      )}

      {subscriptions !== null && !failed && visible.length > 0 && (
        <TodayArrivalsSection
          arrivals={todayArrivals}
          failed={todayArrivalsFailed}
          downloadTasks={downloadTasks}
          canOpenTasks={isAdmin}
          mediaType={mediaType}
          trackingCount={visible.filter((sub) => sub.status === "active").length}
          hasTvInView={visible.some((sub) => sub.media.kind === "tv")}
          canSubscribe={canSubscribe}
        />
      )}

      {subscriptions === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载订阅…
        </div>
      )}

      {failed && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">订阅列表加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </div>
      )}

      {subscriptions !== null && !failed && visible.length === 0 && (
        <ContentEmptyState
          variant="subscription"
          title={
            subscriptions.length === 0
              ? "从一部想看的作品开始"
              : `还没有${mediaType === "movie" ? "电影" : "剧集"}订阅`
          }
          description={
            canSubscribe
              ? `去发现页挑选一部${mediaType === "movie" ? "电影" : "剧集"}，打开详情并点击「订阅追踪」，有合适资源时会自动下载入库。`
              : "当前账号暂未开启订阅权限，请联系管理员为你开启。"
          }
          action={
            canSubscribe ? (
              <Link
                href={`/discover/${mediaType === "movie" ? "movie" : "tv"}` as Route}
                className="btn-accent flex items-center gap-1.5 rounded-full px-4 py-2 text-ui font-semibold"
              >
                <CompassIcon className="size-4" />
                去发现{mediaType === "movie" ? "电影" : "剧集"}
              </Link>
            ) : undefined
          }
        />
      )}

      {visible.length > 0 && mediaType === "all" && (
        allSections.map((section) => (
          <SubscriptionSection
            key={`all-${section.kind}`}
            title={section.title}
            kind={section.kind}
            subscriptions={section.subscriptions}
            ruleSetNameById={ruleSetNameById}
            libraryNameById={libraryNameById}
            defaultLibraryNameByKind={defaultLibraryNameByKind}
          />
        ))
      )}
      {visible.length > 0 && mediaType !== "all" && (
        <SubscriptionSection
          key={`filtered-${mediaType}`}
          kind={mediaType}
          subscriptions={visible}
          ruleSetNameById={ruleSetNameById}
          libraryNameById={libraryNameById}
          defaultLibraryNameByKind={defaultLibraryNameByKind}
        />
      )}
    </div>
  );
}

/**
 * 一个订阅分区：首次只挂载有限数量海报，距离分区末端约一屏时提前追加。
 * 标题只用于“全部”总览；sticky 受 section 边界约束，下一分区会自然将它顶走。
 */
function SubscriptionSection({
  title,
  kind,
  subscriptions,
  ruleSetNameById,
  libraryNameById,
  defaultLibraryNameByKind,
}: {
  title?: string;
  kind: SubscriptionKind;
  subscriptions: Subscription[];
  ruleSetNameById: ReadonlyMap<number, string>;
  libraryNameById: ReadonlyMap<number, string>;
  defaultLibraryNameByKind: ReadonlyMap<string, string>;
}) {
  const [visibleCount, setVisibleCount] = useState(() => rememberedVisibleCounts[kind]);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const hasMore = visibleCount < subscriptions.length;

  useEffect(() => {
    rememberedVisibleCounts[kind] = Math.max(rememberedVisibleCounts[kind], visibleCount);
  }, [kind, visibleCount]);

  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || !hasMore) return;
    const scrollRoot = target.closest<HTMLElement>("[data-scroll-root]");
    if (!scrollRoot) return;
    const preloadDistance = Math.max(800, Math.round(scrollRoot.clientHeight * 1.25));
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        setVisibleCount((count) => nextSubscriptionVisibleCount(count, subscriptions.length));
      },
      {
        root: scrollRoot,
        rootMargin: `${preloadDistance}px 0px`,
      },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, subscriptions.length, visibleCount]);

  return (
    <section
      className={title ? "mt-7" : "mt-6"}
      aria-labelledby={title ? `${kind}-subscriptions-title` : undefined}
    >
      {title && (
        <header className="sticky top-2 z-20 mb-4 px-6 max-md:px-4">
          <div className="flex items-center justify-between rounded-2xl border border-white/[0.09] bg-[linear-gradient(145deg,rgba(18,21,30,0.88),rgba(10,12,18,0.82))] px-4 py-2.5 shadow-[0_10px_28px_rgba(0,0,0,0.24),inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-xl">
            {/* 只留标题：分组名已经说清是剧集还是电影，再加一个彩色圆点既没有
                信息量，还会和海报上表示“追更中 / 在库”的绿点撞语义。 */}
            <h3
              id={`${kind}-subscriptions-title`}
              className="text-ui font-semibold tracking-[-0.01em] text-white/90"
            >
              {title}
            </h3>
            <span className="tnum rounded-full border border-white/[0.07] bg-white/[0.04] px-2.5 py-0.5 text-caption text-[var(--text-muted)]">
              {subscriptions.length} 部
            </span>
          </div>
        </header>
      )}
      <div className="grid gap-x-4 gap-y-7 px-6 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:px-4 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
        {subscriptions.slice(0, visibleCount).map((sub) => (
          <div
            key={sub.id}
            data-subscription-id={sub.id}
            // 已经到手的压暗一档：海报墙是用来扫的，用户扫它是想知道"还差
            // 什么"。绿色角标回答单张卡，压暗回答一整屏——亮着的就是还没到
            // 手的那些。悬浮恢复满亮度，压暗只排序视线，不把内容藏起来。
            className={
              subscriptionFullyCollected(sub)
                ? "opacity-[0.72] transition-opacity duration-200 focus-within:opacity-100 hover:opacity-100"
                : undefined
            }
          >
            <SubscriptionCell
              sub={sub}
              ruleSetName={ruleSetNameById.get(sub.rule_set_id)}
              libraryName={
                sub.library_id === null
                  ? defaultLibraryNameByKind.get(sub.media.kind)
                  : libraryNameById.get(sub.library_id)
              }
            />
          </div>
        ))}
      </div>
      {hasMore && <div ref={loadMoreRef} className="h-px" aria-hidden="true" />}
    </section>
  );
}

/**
 * 首页首屏的待入库摘要。这里刻意不放海报与下载百分比：标题负责识别，
 * “下载中”深链到任务中心查看完整下载细节，避免和下方订阅海报墙重复。
 *
 * 这块的目标是回答一个问题：“我下一次能看到新东西是什么时候”。因此它永远
 * 有话说——今天有安排讲今天，没有就讲后端回退给出的最近一天（``daysAhead`` > 0），
 * 一周内都没有则说明订阅仍在追踪。空着不说话会让用户以为功能坏了。
 */
function TodayArrivalsSection({
  arrivals,
  failed,
  downloadTasks,
  canOpenTasks,
  mediaType,
  trackingCount,
  hasTvInView,
  canSubscribe,
}: {
  arrivals: TodaySubscriptionArrival[] | null;
  failed: boolean;
  downloadTasks: DownloadTask[];
  canOpenTasks: boolean;
  mediaType: SubscriptionFilter;
  /** 当前分区里仍在追踪的订阅数，用于空态说明“系统还在盯着”。 */
  trackingCount: number;
  /** 当前分区里有没有剧集：没有就别用“定档”这种剧集语汇跟纯电影用户说话。 */
  hasTvInView: boolean;
  canSubscribe: boolean;
}) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const taskByHash = useMemo(
    () => new Map(downloadTasks.map((task) => [task.info_hash.toLowerCase(), task])),
    [downloadTasks],
  );
  const rows = useMemo(() => {
    // 预告跟随当前分区：切到电影就只看电影，避免和下方海报墙讲不同的事。
    const presented = (arrivals ?? [])
      .filter((arrival) => mediaType === "all" || arrival.media_kind === mediaType)
      .map((arrival) => {
        const task = arrival.info_hash
          ? taskByHash.get(arrival.info_hash.toLowerCase())
          : undefined;
        return {
          arrival,
          presentation: todayArrivalPresentation(arrival, task, now),
        };
      });
    const groups = groupTodayArrivals(presented).toSorted((left, right) => {
      const leftTime = left.presentation.estimatedAt ?? Number.MAX_SAFE_INTEGER;
      const rightTime = right.presentation.estimatedAt ?? Number.MAX_SAFE_INTEGER;
      if (leftTime !== rightTime) return leftTime - rightTime;
      return left.firstWantedId - right.firstWantedId;
    });
    // 后端按媒体类型各自给了焦点日，“全部”分区可能同时拿到两类的不同日子；
    // 这里再收敛一次到最近的那一天，保证卡片始终只讲一件事。
    const nearest = Math.min(...groups.map((group) => group.daysAhead));
    return groups.filter((group) => group.daysAhead === nearest);
  }, [arrivals, mediaType, now, taskByHash]);

  const daysAhead = rows[0]?.daysAhead ?? 0;
  const isUpcoming = rows.length > 0 && daysAhead > 0;
  const episodeCount = rows.reduce((total, row) => total + row.episodeCount, 0);

  return (
    <section
      aria-labelledby="today-arrivals-title"
      className="relative mx-6 mt-5 rounded-[22px] shadow-[0_18px_55px_rgba(0,0,0,0.2)] max-md:mx-4 max-md:mt-4"
    >
      <div className="relative isolate overflow-hidden rounded-[22px] border border-white/[0.1] bg-[linear-gradient(145deg,rgba(16,19,29,0.82),rgba(8,10,16,0.68))] shadow-[inset_0_1px_0_rgba(255,255,255,0.035)] backdrop-blur-xl">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute -right-16 -top-24 size-56 rounded-full bg-violet-500/[0.09] blur-3xl"
        />
        <span
          aria-hidden="true"
          className="pointer-events-none absolute -left-20 top-10 size-44 rounded-full bg-cyan-400/[0.045] blur-3xl"
        />
        <header className="relative flex items-center justify-between gap-4 border-b border-white/[0.065] px-5 py-4 max-md:px-4 max-md:py-3.5">
          <div className="flex min-w-0 items-center gap-3">
            {/* 图标跟着模式走：今天是“以小时计”的时钟，预告是“以天计”的日历。
                预告态收成中性灰——紫/青/琥珀/绿在这张卡里都已经是状态色，
                给“还没到”再占一个色相只会稀释状态语义，压暗才是它该有的分量。 */}
            <span
              className={`grid size-9 shrink-0 place-items-center rounded-xl border border-white/[0.09] bg-white/[0.055] shadow-[inset_0_1px_0_rgba(255,255,255,0.055)] transition-colors duration-300 max-md:size-8 max-md:rounded-lg ${
                isUpcoming ? "text-white/55" : "text-violet-200"
              }`}
            >
              {isUpcoming ? (
                <CalendarIcon className="size-[18px] max-md:size-4" />
              ) : (
                <ClockIcon className="size-[18px] max-md:size-4" />
              )}
            </span>
            <div className="min-w-0">
              <h3 id="today-arrivals-title" className="text-ui font-semibold text-white/92">
                {isUpcoming ? "即将入库" : "今日可能入库"}
              </h3>
              <p className="mt-0.5 truncate text-caption text-white/38">
                {isUpcoming
                  ? "今天没有更新，这是最近的一次"
                  : "播出、出种与下载状态动态估算"}
              </p>
            </div>
          </div>
          {/* 空态不显示“0 部 · 0 集”——那看着像报错，而不是“今天没安排”。 */}
          {arrivals !== null && !failed && rows.length > 0 && (
            <span className="tnum shrink-0 rounded-full border border-white/[0.075] bg-white/[0.045] px-2.5 py-1 text-caption text-white/48 shadow-[inset_0_1px_0_rgba(255,255,255,0.035)]">
              {isUpcoming
                ? `${daysAhead === 1 ? "明天" : `${daysAhead} 天后`} · ${rows.length} 部`
                : `${rows.length} 部 · ${episodeCount} 集`}
            </span>
          )}
        </header>

        {arrivals === null && !failed && (
          <p className="relative px-5 py-5 text-sub text-[var(--text-muted)] max-md:px-4">
            正在计算预计入库时间…
          </p>
        )}
        {failed && (
          <p className="relative px-5 py-5 text-sub text-amber-100/70 max-md:px-4">
            入库预告暂时不可用
          </p>
        )}
        {/* 一周内确实无事可预告时，也要交代清楚系统的状态和用户的下一步，
            不能只留一句“暂无”让人怀疑追踪停了。主句说结论，次句说依据。 */}
        {!failed && arrivals !== null && rows.length === 0 && (
          <div className="relative px-5 py-5 max-md:px-4">
            <p className="text-sub text-white/68">
              {trackingCount === 0
                ? "订阅都已收齐或暂停了"
                : hasTvInView
                  ? "接下来 7 天没有已定档的更新"
                  : "当前没有正在下载或整理的电影"}
            </p>
            <p className="mt-1 text-caption text-white/38">
              {trackingCount > 0 ? (
                <>
                  <span className="tnum">{trackingCount}</span> 部订阅仍在追踪
                  {hasTvInView
                    ? "，定档或出种后会自动开抓"
                    : "，找到合适资源就会自动开抓"}
                </>
              ) : canSubscribe ? (
                <Link
                  href={`/discover/${mediaType === "movie" ? "movie" : "tv"}` as Route}
                  className="text-violet-200/85 underline-offset-4 transition-colors hover:text-violet-100 hover:underline"
                >
                  去发现页添加新的追踪目标 →
                </Link>
              ) : (
                "有新的追踪目标时，这里会提前预告"
              )}
            </p>
          </div>
        )}
        {!failed && rows.length > 0 && (
          <div className="relative space-y-1 p-3">
            {rows.map(({ subscriptionId, mediaTitle, episodeLabel, presentation }, index) => {
            const style = todayArrivalStyle[presentation.statusLabel];
            const statusClass = `inline-flex shrink-0 items-center gap-0.5 rounded-full border px-2 py-0.5 text-caption font-medium transition-colors ${style.status}`;
            return (
              <div
                key={subscriptionId}
                className="group/today grid grid-cols-[1.5rem_minmax(0,1fr)_auto] items-center gap-x-3 rounded-xl px-3 py-3 text-sub transition duration-200 hover:translate-x-0.5 hover:bg-white/[0.042] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.025)] max-md:grid-cols-[1.25rem_minmax(0,1fr)] max-md:gap-x-2.5 max-md:px-2.5"
              >
                <span className="relative flex h-full self-stretch items-start justify-center pt-1.5 max-md:row-span-2">
                  {index < rows.length - 1 && (
                    <span
                      aria-hidden="true"
                      className="absolute left-1/2 top-4 -bottom-5 w-px -translate-x-1/2 bg-gradient-to-b from-white/16 via-white/10 to-white/[0.035]"
                    />
                  )}
                  {/* pulse 只对「下载中」为真，颜色跟着它的 --info 走 */}
                  {style.pulse && (
                    <span
                      aria-hidden="true"
                      className="absolute top-1 size-3 animate-ping rounded-full bg-[var(--info)]/25"
                    />
                  )}
                  {/* 预告是“几天后”，节点压暗一档；发光的实心点留给马上要落地的内容。 */}
                  <span
                    aria-hidden="true"
                    className={`relative z-10 size-2.5 rounded-full border-2 ${style.node}${
                      isUpcoming ? " opacity-65" : ""
                    }`}
                  />
                </span>
                <div className="min-w-0 self-center">
                  <Link
                    href={`/subscriptions/${subscriptionId}` as Route}
                    className="block min-w-0 truncate font-medium text-white/86 transition-colors group-hover/today:text-white"
                  >
                    {mediaTitle}
                  </Link>
                  <span className="tnum mt-0.5 block min-w-0 truncate text-caption text-white/38">
                    {episodeLabel}
                  </span>
                </div>
                <div className="flex flex-col items-end gap-1 max-md:col-start-2 max-md:row-start-2 max-md:mt-1.5 max-md:flex-row max-md:items-center max-md:gap-2">
                  {presentation.statusLabel === "下载中" && canOpenTasks ? (
                    <Link href={"/activity?view=active" as Route} className={statusClass}>
                      下载中
                      <ChevronRightIcon className="size-3" />
                    </Link>
                  ) : (
                    <span className={statusClass}>{presentation.statusLabel}</span>
                  )}
                  <span
                    className={`tnum whitespace-nowrap text-[15px] font-semibold tracking-[-0.01em] transition-colors ${style.time}`}
                  >
                    {presentation.timeLabel}
                  </span>
                </div>
              </div>
            );
            })}
          </div>
        )}
      </div>
    </section>
  );
}

/** 订阅类型切换：沿用发现页的数据源切换样式，让同类操作保持一致。 */
function MediaTypeSwitcher({
  value,
  onChange,
  compact = false,
}: {
  value: SubscriptionFilter;
  onChange: (type: SubscriptionFilter) => void;
  compact?: boolean;
}) {
  const labels: Record<SubscriptionFilter, string> = {
    all: "全部",
    tv: "剧集",
    movie: "电影",
  };
  return (
    <div
      className="flex shrink-0 rounded-full border border-white/10 bg-black/35 p-1 backdrop-blur-xl"
      aria-label="订阅类型"
    >
      {(["all", "tv", "movie"] as const).map((type) => (
        <button
          key={type}
          type="button"
          aria-pressed={value === type}
          onClick={() => onChange(type)}
          className={`rounded-full py-1.5 text-sub font-semibold transition ${compact ? "px-2.5" : "px-4"} ${
            value === type
              ? "bg-white/15 text-white shadow-sm"
              : "text-[var(--text-muted)] hover:text-white"
          }`}
        >
          {labels[type]}
        </button>
      ))}
    </div>
  );
}

/** 连续季压成 S1–S3，离散季保留为 S1 · S3，避免悬浮层变成长句。 */
function compactSeasonRange(seasons: number[]): string | undefined {
  const values = [...new Set(seasons)].sort((a, b) => a - b);
  if (values.length === 0) return undefined;
  const ranges: string[] = [];
  let start = values[0];
  let end = values[0];
  for (const value of values.slice(1)) {
    if (value === end + 1) {
      end = value;
      continue;
    }
    ranges.push(start === end ? `S${start}` : `S${start}–S${end}`);
    start = value;
    end = value;
  }
  ranges.push(start === end ? `S${start}` : `S${start}–S${end}`);
  return ranges.join(" · ");
}

/** 把订阅配置压成海报卡片需要的短信息，不再混入运行状态与任务进度。 */
function toVisualItem(
  sub: Subscription,
  ruleSetName?: string,
  libraryName?: string,
): PosterVisualItem {
  const scope = sub.media.kind === "tv" ? compactSeasonRange(sub.selected_seasons) : undefined;
  const configFlow = [ruleSetName, libraryName].filter(Boolean).join("  →  ");
  const ribbon = subscriptionRibbon(sub);
  return {
    id: String(sub.media.tmdb_id),
    source: "tmdb",
    type: sub.media.kind,
    title: sub.media.title,
    year: sub.media.year ?? undefined,
    rating: 0,
    posterUrl: sub.media.poster_url ? cachedImageUrl(sub.media.poster_url) : "",
    genres: scope ? [scope] : undefined,
    overlayMeta: configFlow || undefined,
    ribbon: ribbon?.label,
    ribbonTone: ribbon?.tone,
    // 角标一律用紧凑左上斜标：右上角让给「洗 N」洗版徽标（常驻状态数量比
    // 能力开关更值得占主视觉位）
    ribbonVariant: "compact-left",
    posterFooter: subscriptionCollectionMeta(sub),
  };
}

/** 海报墙单元格：点击进订阅详情分析页（追踪明细 + 活动时间线），而非影片详情。 */
function SubscriptionCell({
  sub,
  ruleSetName,
  libraryName,
}: {
  sub: Subscription;
  ruleSetName?: string;
  libraryName?: string;
}) {
  return (
    <PosterCardVisual
      item={toVisualItem(sub, ruleSetName, libraryName)}
      href={`/subscriptions/${sub.id}` as Route}
      action="none"
      revealInfoOnTouch
    />
  );
}
