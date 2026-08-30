"use client";

import Link from "next/link";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { useConfirm, useToast } from "@/components/feedback";
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  FilmIcon,
  MoreIcon,
  RefreshIcon,
  SearchIcon,
} from "@/components/icons";
import { MediaSourceAnnotationDialog } from "@/components/media-source-annotation-dialog";
import { Modal } from "@/components/modal";
import { PageNav } from "@/components/page-nav";
import { usePageTitle } from "@/lib/use-page-title";
import { PosterImage } from "@/components/poster-image";
import { specSummary, upgradeTargetLabel } from "@/components/rule-sets-panel";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { SubscriptionAdjustDialog } from "@/components/subscription-adjust-dialog";
import { UpgradeRunDialog } from "@/components/upgrade-run-dialog";
import {
  deleteSubscriptionPermanently,
  getSubscription,
  listActiveSubscriptionDownloads,
  listRuleSets,
  listSubscriptionActivities,
  runSubscriptionUpgradeRound,
  searchMissingSubscriptionResources,
  setSubscriptionFollowFuture,
  setSubscriptionTrackingState,
  unsubscribeFromSubscription,
  updateSubscription,
  type RuleSet,
  type SubscriptionActivity,
  type SubscriptionDetail,
  type SubscriptionDownload,
  type ResourceTiming,
  type WantedItem,
} from "@/lib/api/subscriptions";
import { formatBytes, formatDuration } from "@/lib/format";
import { cachedImageUrl } from "@/lib/image-proxy";
import { seasonsWithIndeterminate } from "@/lib/media-source-annotation";
import { shouldShowResourceTiming } from "@/lib/resource-timing";
import { subscriptionStatusMeta } from "@/lib/subscription-ui";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { usePermissions } from "@/lib/permissions";

/**
 * 订阅详情分析页（/subscriptions/[id]）：订阅透明化的落点。
 *
 * 页面结构（与影片详情页同一套视觉语言）：
 *   1. Hero 氛围横幅 —— 海报低透明度取色产出该片专属底色（订阅接口无宽幅
 *      剧照，海报是永远可用的兜底），上面按状态 / 海报身份 / 配置参数 /
 *      收录进度 / 操作区纵向组织；移动端配置与操作跨满海报下方；
 *   2. 单视图主体 —— 「一集一条履历」：不再分「追踪明细 / 活动记录」两个
 *      标签页，而是把每集展开成一条固定形状的里程碑链（播出 → 搜索 → 投递
 *      → 下载 → 入库 →（洗版）），第一个非完成节点就是这集卡住的地方。
 *      链的六站时间戳全在工单（WantedItem）上，一次详情请求即得；叙事细节
 *      （最近被拒原因 / 投递种子名）是工单上的冗余快照两列，不再回活动流水
 *      里按季集捞。原活动流水按性质归位：
 *      - 搜索轮次（订阅级，一轮服务所有缺口，天然拆不到集）→ 顶部一行摘要，
 *        只留最近一轮；
 *      - 集级事件 → 降级为链上节点的注解；
 *      - 全量流水 → 底部「排查记录」折叠区，日常不看、排查时下钻原始记录。
 */
export function SubscriptionInspectorView({
  id,
  autoOpenUpgradeRun = false,
}: {
  id: number;
  /** 进入即打开「洗一轮版」弹层（库详情洗版入口跳转并轨到既有订阅，§13.4） */
  autoOpenUpgradeRun?: boolean;
}) {
  const router = useRouter();
  const confirm = useConfirm();
  // 暂停/取消订阅会改变全站订阅状态（海报卡片的「已订阅」徽标），操作后同步刷新
  const { canSubscribe, refresh: refreshSubscriptions } = useSubscribeEntry();
  const { canManageSubscriptions, canSearch, isAdmin } = usePermissions();
  const [detail, setDetail] = useState<SubscriptionDetail | null>(null);
  const [activities, setActivities] = useState<SubscriptionActivity[]>([]);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [switchingRule, setSwitchingRule] = useState(false);
  const [adjusting, setAdjusting] = useState(false);
  const [upgradeRunning, setUpgradeRunning] = useState(autoOpenUpgradeRun);
  const [managing, setManaging] = useState(false);
  const toast = useToast();

  const [downloads, setDownloads] = useState<SubscriptionDownload[]>([]);

  const reload = useCallback(() => {
    Promise.all([
      getSubscription(id),
      listSubscriptionActivities(id),
      canManageSubscriptions ? listRuleSets() : Promise.resolve([]),
    ])
      .then(([d, acts, rules]) => {
        setDetail(d);
        setActivities(acts);
        setRuleSets(rules);
      })
      .catch(() => setFailed(true));
  }, [canManageSubscriptions, id]);

  useEffect(() => {
    reload();
  }, [reload]);

  // 消费掉 ?upgrade-run=1：弹层已按初始态打开，把参数从地址栏摘掉，
  // 避免关闭弹层后刷新页面又弹一次
  useEffect(() => {
    if (autoOpenUpgradeRun) {
      router.replace(`/subscriptions/${id}` as Route, { scroll: false });
    }
  }, [autoOpenUpgradeRun, id, router]);

  // 有在途投递（已提交下载/已下载待入库）时才轮询：
  // - 5s 拉一次实时进度（速度/ETA，纯读快照）；
  // - 30s 静默刷一次详情，接住"下载完成→入库→工单关闭"的状态跃迁。
  // 页面隐藏自动暂停（useVisiblePolling）；无在途工单时零请求。
  const hasInFlight =
    detail?.wanted.some((w) => w.status === "grabbed" || w.status === "downloaded") ?? false;
  // 在途请求守卫：下载器响应慢于轮询间隔时跳过本 tick，
  // 避免请求堆叠、以及慢的旧响应乱序覆盖新数据
  const downloadsPending = useRef(false);
  useVisiblePolling(
    () => {
      if (downloadsPending.current) return;
      downloadsPending.current = true;
      listActiveSubscriptionDownloads(id)
        .then(setDownloads)
        .catch(() => undefined)
        .finally(() => {
          downloadsPending.current = false;
        });
    },
    hasInFlight ? 5000 : null,
    { leading: true },
  );
  useVisiblePolling(reload, hasInFlight ? 30000 : null);

  // 工单 → 进度组的索引（同一整季包的多集共享一个种子/一份进度）
  const downloadByHash = useMemo(() => {
    const map = new Map<string, SubscriptionDownload>();
    for (const d of downloads) map.set(d.info_hash, d);
    return map;
  }, [downloads]);

  const ruleSetName = useMemo(() => {
    const rs = ruleSets.find((r) => r.id === detail?.rule_set_id);
    if (!rs) return `#${detail?.rule_set_id}`;
    // 规则组配了洗版目标时在 fact 里带出（quality-upgrade.md §8.3）
    const target = upgradeTargetLabel(rs.spec);
    return target ? `${rs.name} · 洗到 ${target}` : rs.name;
  }, [ruleSets, detail]);
  usePageTitle(detail?.media.title);

  // 兜底态（加载中/失败）也渲染 PageNav（片名未知，末项留空）：向外壳登记
  // 「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失、顶部闪一下。
  const navFallback = { label: "我的订阅", href: "/subscriptions" as Route };

  if (failed) {
    return (
      <div className="flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <div className="flex flex-1 flex-col items-center justify-center gap-4">
          <p className="text-body text-[var(--text-muted)]">未能加载该订阅，可能已被删除。</p>
          <Link
            href="/subscriptions"
            replace
            className="btn-glass px-4 py-2 text-ui font-medium"
          >
            <ArrowLeftIcon className="size-4" />
            返回订阅列表
          </Link>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载订阅详情…
        </div>
      </div>
    );
  }

  const meta = subscriptionStatusMeta[detail.status];
  // 复合态（quality-upgrade.md §8.3）：内容收齐了但还有单元在洗更高版本。
  // 「已收齐」语义不变（洗版不影响完成判定），只在文案上如实补一句
  const upgradingCount = detail.progress.upgrading ?? 0;
  // 电影只有一个单元，「洗版中（1）」读着别扭——单单元不带计数
  const upgradingText =
    detail.progress.total > 1 ? `洗版中（${upgradingCount}）` : "洗版中";
  const statusLabel =
    detail.status === "completed" && upgradingCount > 0
      ? `已收齐 · ${upgradingText}`
      : detail.status === "active" && upgradingCount > 0 && detail.progress.wanted === 0
        ? `${meta.label} · ${upgradingText}`
        : meta.label;
  const isMovie = detail.media.kind === "movie";
  const poster = detail.media.poster_url ? cachedImageUrl(detail.media.poster_url) : null;

  const togglePause = async () => {
    const resuming = detail.status === "paused";
    const ok = await confirm({
      title: resuming
        ? `恢复《${detail.media.title}》的订阅追踪？`
        : `暂停《${detail.media.title}》的订阅追踪？`,
      description: resuming
        ? "恢复后会继续搜索缺失资源，并按当前规则自动投递符合条件的结果。"
        : "暂停后不会继续搜索或投递资源；已经提交的下载不受影响，可随时恢复。",
      confirmLabel: resuming ? "恢复追踪" : "暂停追踪",
      cancelLabel: "返回",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await setSubscriptionTrackingState(
        detail.id,
        resuming ? "active" : "paused",
      );
      toast.success(resuming ? "已恢复订阅追踪" : "已暂停订阅追踪");
      reload();
      refreshSubscriptions();
    } finally {
      setBusy(false);
    }
  };

  /** 自动续订是可逆的单一状态动作，详情页直接切换，不再打开批量调整弹窗。 */
  const toggleFollowFuture = async () => {
    const enabling = !detail.follow_future;
    setBusy(true);
    try {
      await setSubscriptionFollowFuture(detail.id, enabling);
      toast.success(enabling ? "已开启自动续订" : "已关闭自动续订");
      reload();
      refreshSubscriptions();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "设置自动续订失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  // 立即搜索：缺口跳过冷却重新排队。后端对"暂停中/没有可搜缺口"给可读错误，
  // 原样进 toast——按钮不做前置禁用判断，语义由唯一实现（服务端）说了算
  const searchNow = async () => {
    const ok = await confirm({
      title: `立即搜索《${detail.media.title}》的缺失资源？`,
      description:
        "将跳过当前搜索冷却，重新搜索已经可以搜索的缺口。命中当前规则组的资源可能会自动提交下载。",
      confirmLabel: "立即搜索",
      cancelLabel: "返回",
    });
    if (!ok) return;
    setBusy(true);
    try {
      const { reset_count } = await searchMissingSubscriptionResources(detail.id);
      toast.success(`${reset_count} 个缺口已重新排队，正在搜索`);
      reload();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "触发搜索失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: `取消订阅《${detail.media.title}》？`,
      description: isAdmin
        ? "将停止追踪剩余内容；已经下载或入库的文件不会被删除。"
        : "将取消你的订阅关注；已经下载或入库的文件不会被删除。",
      confirmLabel: "取消订阅",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      if (isAdmin) await deleteSubscriptionPermanently(detail.id);
      else await unsubscribeFromSubscription(detail.id);
      refreshSubscriptions();
      // 订阅已不存在，替换当前历史项，避免浏览器后退再次进入失效详情。
      router.replace("/subscriptions");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto px-6 pb-12 max-md:px-4">
      {/* 顶栏：返回订阅列表 + 吸顶片名（容器已有 px-6，用 -mx-6 让吸顶蒙版铺满） */}
      <PageNav
        title={detail.media.title}
        fallback={navFallback}
        className="-mx-6 max-md:-mx-4"
      />
      {/* —— 1. 订阅摘要卡：状态、身份、配置、进度、操作自上而下形成单一路径。
          海报仍提供条目辨识与轻量氛围，但背景只低透明度取色，不再压过正文。
          PageNav 已占一行导航高度，摘要卡再按一级页基线留出桌面 28px / 移动端
          16px 的内容间距，避免重卡片贴住顶栏。 —— */}
      <section className="relative mt-7 overflow-hidden rounded-2xl bg-[#0d111b] shadow-[0_24px_70px_-18px_rgba(0,0,0,0.58)] ring-1 ring-white/10 max-md:mt-4">
        {poster && (
          <PosterImage
            src={poster}
            alt=""
            className="absolute inset-0 size-full scale-110 object-cover opacity-25 blur-3xl brightness-[0.42] saturate-[0.9]"
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-r from-[rgba(7,10,17,0.97)] via-[rgba(9,13,22,0.91)] to-[rgba(10,14,23,0.84)]" />

        {/* 卡片眉头只回答两件事：订阅类型与当前状态。进度挪到主体单独表达，
            不再把“追踪中 · 缺几集 · 已入库几集”揉成一条长句。 */}
        <div className="relative z-10 flex items-center justify-between gap-4 px-6 pt-5 max-md:px-4 max-md:pt-4">
          <p className="text-caption font-semibold tracking-[0.2em] text-[var(--accent-2)]">
            {isMovie ? "电影订阅" : "剧集订阅"}
          </p>
          <span className="flex shrink-0 items-center gap-2 text-sub text-white/65">
            <span
              className="size-1.5 rounded-full shadow-[0_0_0_4px_rgba(255,255,255,0.05)]"
              style={{ backgroundColor: meta.color }}
            />
            {statusLabel}
          </span>
        </div>

        <div className="relative z-10 grid grid-cols-[112px_minmax(0,1fr)] gap-5 px-6 pb-6 pt-4 max-md:grid-cols-[80px_minmax(0,1fr)] max-md:gap-4 max-md:px-4 max-md:pb-4 max-md:pt-3">
          <Link
            href={`/media/${detail.media.kind}/${detail.media.tmdb_id}`}
            aria-label={`查看《${detail.media.title}》详情`}
            className="block w-28 shrink-0 self-start overflow-hidden rounded-xl bg-white/[0.05] shadow-[0_16px_40px_rgba(0,0,0,0.45)] ring-1 ring-white/15 max-md:w-20 max-md:rounded-lg"
          >
            {poster ? (
              <PosterImage
                src={poster}
                alt={`${detail.media.title} 海报`}
                className="aspect-[2/3] w-full object-cover"
              />
            ) : (
              <span className="flex aspect-[2/3] flex-col items-center justify-center gap-2 text-caption text-white/35">
                <FilmIcon className="size-6" />
                暂无海报
              </span>
            )}
          </Link>

          {/* display:contents 让移动端的配置、进度和按钮跨满海报下方；桌面端
              恢复为普通纵向内容列，操作区自然沉到右下，不再形成第三列。 */}
          <div className="contents md:flex md:min-w-0 md:flex-col">
            <div className="min-w-0 self-center md:self-auto">
              <h1 className="flex min-w-0 items-baseline gap-2.5 text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[20px]">
                <Link
                  href={`/media/${detail.media.kind}/${detail.media.tmdb_id}`}
                  className="min-w-0 truncate hover:underline"
                >
                  {detail.media.title}
                </Link>
                <span className="tnum shrink-0 text-body font-normal text-white/45">
                  {detail.media.year ?? ""}
                </span>
              </h1>
              <p className="mt-2 flex flex-wrap gap-x-2 gap-y-1 text-sub text-white/55">
                {detail.media.original_title &&
                  detail.media.original_title !== detail.media.title && (
                    <span className="truncate">{detail.media.original_title}</span>
                  )}
                <span className="shrink-0">订阅于 {formatDateTime(detail.created_at)}</span>
              </p>
            </div>

            {/* 配置不再做成一排同款胶囊，而是稳定的 label/value 信息列。
                每列保持可扫读；超长季列表或规则名只在值行截断。 */}
            <div className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(80px,1fr))] gap-3 border-y border-white/[0.08] py-3 max-md:col-span-2 max-md:mt-1">
              <SubscriptionFact
                label="收录范围"
                value={
                  isMovie
                    ? "正片"
                    : detail.selected_seasons.length > 0
                      ? `第 ${detail.selected_seasons.join("、")} 季`
                      : "未勾选季"
                }
              />
              {!isMovie && (
                <SubscriptionFact
                  label="自动续订"
                  value={detail.follow_future ? "已开启" : "已关闭"}
                />
              )}
              {canManageSubscriptions && <SubscriptionFact label="规则组" value={ruleSetName} />}
            </div>

            <ProgressStrip progress={detail.progress} unitLabel={isMovie ? "部" : "集"} />

            {/* 一套 DOM 同时服务桌面与移动端：桌面右对齐、移动端等宽单行。
                只有“更多”的承载物因交互范式不同分为下拉菜单与底部抽屉。 */}
            <div className="mt-auto flex flex-wrap justify-end gap-2 pt-4 max-md:col-span-2 max-md:grid max-md:grid-flow-col max-md:auto-cols-fr max-md:pt-3">
              {/* 缺口存在且未暂停时才有意义；其余情况后端会给可读错误，按钮直接隐藏更干净 */}
              {canSubscribe && detail.progress.wanted > 0 && detail.status !== "paused" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void searchNow()}
                  className="btn-accent inline-flex h-10 min-w-0 items-center justify-center gap-1.5 rounded-full px-4 text-sub font-semibold disabled:opacity-40 max-md:h-11 max-md:px-2"
                >
                  <RefreshIcon className="size-4 shrink-0" />
                  <span className="whitespace-nowrap">立即搜索</span>
                </button>
              )}
              {/* 有缺口，或配了洗版目标且有已入库单元（手选换版本，§13.8）时展示 */}
              {canSubscribe &&
                canSearch &&
                (detail.progress.wanted > 0 || detail.wanted.some((w) => w.upgrade)) && (
                <Link
                  href={
                    `/search?q=${encodeURIComponent(detail.media.title)}&for_sub=${detail.id}` as Route
                  }
                  className="btn-glass inline-flex h-10 min-w-0 items-center justify-center gap-1.5 border border-white/10 bg-white/[0.05] px-4 text-sub font-medium backdrop-blur-md max-md:h-11 max-md:px-2"
                  title="到站点资源搜索里挑一条种子，直接投给本订阅（跳过规则组限制；替换已入库版本时按入库实测裁决，证明更优才替换）"
                >
                  <SearchIcon className="size-4 shrink-0" />
                  <span className="whitespace-nowrap">手动选种</span>
                </Link>
              )}
              {(canSubscribe || canManageSubscriptions) && (
                <>
                  <span className="max-md:hidden">
                    <SubscriptionManageMenu
                      busy={busy}
                      paused={detail.status === "paused"}
                      completed={detail.status === "completed"}
                      canSubscribe={canSubscribe}
                      canManageSubscriptions={canManageSubscriptions}
                      followFuture={isMovie ? null : detail.follow_future}
                      onAdjust={() => setAdjusting(true)}
                      onUpgradeRun={() => setUpgradeRunning(true)}
                      onToggleFollowFuture={() => void toggleFollowFuture()}
                      onSwitchRule={() => setSwitchingRule(true)}
                      onTogglePause={() => void togglePause()}
                      onRemove={() => void remove()}
                    />
                  </span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setManaging(true)}
                    className="hidden h-11 min-w-0 items-center justify-center gap-1.5 rounded-full bg-white/[0.035] px-2 text-sub font-medium text-white/65 transition hover:bg-white/[0.09] hover:text-white disabled:opacity-40 max-md:flex"
                  >
                    <MoreIcon className="size-4 shrink-0" />
                    <span className="whitespace-nowrap">更多</span>
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </section>

      {/* —— 2. 单视图主体：搜索轮次摘要 → 按季分组的集履历 → 排查记录 —— */}
      <div className="mt-7 space-y-4">
        <SearchRoundBar activities={activities} wanted={detail.wanted} />
        <WantedBreakdown
          wanted={detail.wanted}
          isMovie={isMovie}
          downloads={downloadByHash}
          failures={pendingFailures(activities)}
          mediaItemId={detail.media.media_item_id}
          canAnnotate={isAdmin}
          onAnnotated={async (message) => {
            // 标注已刷新快照：顺手重跑一轮体检完成排期（幂等），再刷新详情
            try {
              await runSubscriptionUpgradeRound(id);
              toast.success(`${message}，已重新体检并排期洗版`);
            } catch {
              toast.success(message);
            }
            reload();
          }}
        />
        <ActivityLogSection activities={activities} />
      </div>

      {(canSubscribe || canManageSubscriptions) && managing && (
        <SubscriptionManageSheet
          open
          busy={busy}
          paused={detail.status === "paused"}
          completed={detail.status === "completed"}
          canSubscribe={canSubscribe}
          canManageSubscriptions={canManageSubscriptions}
          followFuture={isMovie ? null : detail.follow_future}
          onClose={() => setManaging(false)}
          onAdjust={() => {
            setManaging(false);
            setAdjusting(true);
          }}
          onUpgradeRun={() => {
            setManaging(false);
            setUpgradeRunning(true);
          }}
          onToggleFollowFuture={() => {
            setManaging(false);
            void toggleFollowFuture();
          }}
          onSwitchRule={() => {
            setManaging(false);
            setSwitchingRule(true);
          }}
          onTogglePause={() => {
            setManaging(false);
            void togglePause();
          }}
          onRemove={() => {
            setManaging(false);
            void remove();
          }}
        />
      )}

      {canSubscribe && adjusting && (
        <SubscriptionAdjustDialog
          detail={detail}
          onClose={() => setAdjusting(false)}
          onSaved={() => {
            setAdjusting(false);
            reload();
            refreshSubscriptions();
          }}
        />
      )}

      {canSubscribe && upgradeRunning && (
        <UpgradeRunDialog
          detail={detail}
          onClose={() => setUpgradeRunning(false)}
          onFinished={() => {
            setUpgradeRunning(false);
            reload();
            refreshSubscriptions();
          }}
        />
      )}

      {canManageSubscriptions && switchingRule && (
        <RuleSetSwitchDialog
          ruleSets={ruleSets}
          currentId={detail.rule_set_id}
          onClose={() => setSwitchingRule(false)}
          onPick={async (ruleSetId) => {
            await updateSubscription(detail.id, { rule_set_id: ruleSetId });
            setSwitchingRule(false);
            reload();
          }}
        />
      )}
    </div>
  );
}

interface SubscriptionManageActionsProps {
  busy: boolean;
  paused: boolean;
  completed: boolean;
  canSubscribe: boolean;
  canManageSubscriptions: boolean;
  /** null 表示电影订阅，不展示没有业务语义的自动续订动作。 */
  followFuture: boolean | null;
  onAdjust: () => void;
  /** 打开「洗一轮版」弹层（quality-upgrade.md §13.5 的订阅详情入口） */
  onUpgradeRun: () => void;
  onToggleFollowFuture: () => void;
  onSwitchRule: () => void;
  onTogglePause: () => void;
  onRemove: () => void;
}

/** 桌面端管理菜单：Portal 与碰撞检测交给 Radix，避免 Hero 的 overflow-hidden
 * 裁掉菜单；资源操作留在外面，配置、状态和危险操作按频率收进这里。 */
function SubscriptionManageMenu({
  busy,
  paused,
  completed,
  canSubscribe,
  canManageSubscriptions,
  followFuture,
  onAdjust,
  onUpgradeRun,
  onToggleFollowFuture,
  onSwitchRule,
  onTogglePause,
  onRemove,
}: SubscriptionManageActionsProps) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={busy}
          className="inline-flex h-9 items-center gap-1.5 rounded-full px-3 text-sub font-medium text-white/65 transition hover:bg-white/[0.08] hover:text-white data-[state=open]:bg-white/[0.1] data-[state=open]:text-white disabled:opacity-40"
        >
          <MoreIcon className="size-4" />
          更多
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[10.5rem] !rounded-xl p-1"
        >
          {canSubscribe && (
            <DropdownMenu.Item onSelect={onAdjust} className={itemClass}>
              调整订阅…
            </DropdownMenu.Item>
          )}
          {canSubscribe && (
            <DropdownMenu.Item onSelect={onUpgradeRun} disabled={busy} className={itemClass}>
              洗一轮版…
            </DropdownMenu.Item>
          )}
          {canSubscribe && followFuture !== null && (
            <DropdownMenu.Item
              onSelect={onToggleFollowFuture}
              disabled={busy}
              className={itemClass}
            >
              {followFuture ? "关闭自动续订" : "开启自动续订"}
            </DropdownMenu.Item>
          )}
          {canManageSubscriptions && (
            <DropdownMenu.Item onSelect={onSwitchRule} className={itemClass}>
              更换规则组…
            </DropdownMenu.Item>
          )}
          {canSubscribe && (
            <DropdownMenu.Item
              onSelect={onTogglePause}
              disabled={busy || completed}
              className={itemClass}
            >
              {paused ? "恢复追踪" : "暂停追踪"}
            </DropdownMenu.Item>
          )}
          {canSubscribe && <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />}
          {canSubscribe && (
            <DropdownMenu.Item
              onSelect={onRemove}
              disabled={busy}
              className={`${itemClass} !text-[#ff8b8b] data-[highlighted]:!bg-[#ff6b6b]/10`}
            >
              取消订阅
            </DropdownMenu.Item>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/** 移动端管理动作使用底部抽屉：每一行都是完整触控目标，危险操作与普通配置
 * 之间用分隔线断开；具体动作的二次确认仍由调用方统一负责。 */
function SubscriptionManageSheet({
  open,
  busy,
  paused,
  completed,
  canSubscribe,
  canManageSubscriptions,
  followFuture,
  onClose,
  onAdjust,
  onUpgradeRun,
  onToggleFollowFuture,
  onSwitchRule,
  onTogglePause,
  onRemove,
}: SubscriptionManageActionsProps & { open: boolean; onClose: () => void }) {
  const rowClass =
    "flex min-h-12 w-full items-center justify-between gap-3 px-4 py-3 text-left text-ui " +
    "font-medium text-white/85 transition hover:bg-white/[0.07] disabled:opacity-40";

  return (
    <Modal open={open} onClose={onClose} label="管理订阅">
      <div className="p-4 pt-3">
        <div aria-hidden className="mx-auto mb-3 h-1 w-10 rounded-full bg-white/20" />
        <h2 className="px-2 text-title-sm font-bold text-white">管理订阅</h2>
        <div className="mt-3 overflow-hidden rounded-xl bg-white/[0.035]">
          {canSubscribe && (
            <button type="button" onClick={onAdjust} className={rowClass}>
              <span>调整订阅</span>
              <span aria-hidden className="text-white/35">›</span>
            </button>
          )}
          {canSubscribe && (
            <button type="button" disabled={busy} onClick={onUpgradeRun} className={rowClass}>
              <span>洗一轮版</span>
              <span aria-hidden className="text-white/35">›</span>
            </button>
          )}
          {canSubscribe && followFuture !== null && (
            <button
              type="button"
              disabled={busy}
              onClick={onToggleFollowFuture}
              className={rowClass}
            >
              <span>{followFuture ? "关闭自动续订" : "开启自动续订"}</span>
            </button>
          )}
          {canManageSubscriptions && (
            <button type="button" onClick={onSwitchRule} className={rowClass}>
              <span>更换规则组</span>
              <span aria-hidden className="text-white/35">›</span>
            </button>
          )}
          {canSubscribe && (
            <button
              type="button"
              disabled={busy || completed}
              onClick={onTogglePause}
              className={rowClass}
            >
              <span>{paused ? "恢复追踪" : "暂停追踪"}</span>
              <span aria-hidden className="text-white/35">›</span>
            </button>
          )}
          {canSubscribe && (
            <button
              type="button"
              disabled={busy}
              onClick={onRemove}
              className={`${rowClass} border-t border-white/[0.07] !text-[#ff8b8b] hover:!bg-[#ff6b6b]/10`}
            >
              <span>取消订阅</span>
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="btn-glass mt-3 h-11 w-full text-ui font-medium"
        >
          关闭
        </button>
      </div>
    </Modal>
  );
}

/**
 * 换规则组弹窗：列出全部规则组（含条件摘要），点选即应用。
 * 只影响之后的资源评估——已投递/已入库的工单不追溯，弹窗里说清楚。
 */
function RuleSetSwitchDialog({
  ruleSets,
  currentId,
  onClose,
  onPick,
}: {
  ruleSets: RuleSet[];
  currentId: number;
  onClose: () => void;
  onPick: (ruleSetId: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pick = async (id: number) => {
    if (id === currentId) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onPick(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "更换失败，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} label="更换规则组" width="lg">
      <div className="scroll-thin max-h-[76dvh] overflow-y-auto p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">更换规则组</h2>
        <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
          点选即应用，只影响之后的资源评估；已下载/已入库的内容不受影响。
          需要新的组合条件可去「设置 → 订阅 → 规则组」新建。
        </p>
        {error && (
          <p className="mt-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-sub leading-6 text-red-200">
            {error}
          </p>
        )}
        <div className="mt-4 space-y-1.5">
          {ruleSets.map((rs) => {
            const chips = specSummary(rs.spec);
            const current = rs.id === currentId;
            return (
              <button
                key={rs.id}
                type="button"
                disabled={busy}
                onClick={() => void pick(rs.id)}
                className={`w-full rounded-xl border px-4 py-2.5 text-left transition disabled:opacity-50 ${
                  current
                    ? "border-white/25 bg-white/[0.1]"
                    : "border-white/[0.08] bg-white/[0.03] hover:bg-white/[0.07]"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-ui font-medium text-white/90">
                    {rs.name}
                    {rs.is_default && (
                      <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                        默认
                      </span>
                    )}
                  </span>
                  {current && (
                    <span className="shrink-0 text-caption font-medium text-[var(--ok)]">
                      当前使用 ✓
                    </span>
                  )}
                </span>
                <span className="mt-1 flex flex-wrap gap-1.5">
                  {chips.length === 0 ? (
                    <span className="text-caption text-[var(--text-faint)]">全不限</span>
                  ) : (
                    chips.map((chip) => (
                      <span
                        key={chip}
                        className="rounded-md bg-white/[0.07] px-1.5 py-0.5 text-caption text-white/75"
                      >
                        {chip}
                      </span>
                    ))
                  )}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </Modal>
  );
}

/** 订阅配置的一列：label 负责稳定定位，value 承载当前值。
 * 不再用一串同形胶囊表达不同维度，避免用户把只读信息误认成按钮。 */
function SubscriptionFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-caption text-white/40">{label}</p>
      <p className="mt-1 truncate text-sub font-medium text-white/85" title={value}>
        {value}
      </p>
    </div>
  );
}

/** 三段式收录进度：绿=已入库、蓝=下载/待入库、底轨=仍缺失。
 * 数字图例与颜色同时表达状态，不让色觉成为理解进度的唯一通道。 */
function ProgressStrip({
  progress,
  unitLabel,
}: {
  progress: SubscriptionDetail["progress"];
  unitLabel: "部" | "集";
}) {
  const { total, wanted, grabbed, downloaded, imported } = progress;
  const denom = Math.max(total, 1);
  const inPipeline = grabbed + downloaded;
  // 洗版中 ⊆ 已入库：内容在手（计入分子），但版本还在向目标档位洗——
  // 条带里用青色从绿色里分出来，图例给数字，不让色觉成为唯一通道
  const upgrading = progress.upgrading ?? 0;
  const importedSettled = Math.max(imported - upgrading, 0);
  return (
    <div className="mt-4 max-md:col-span-2 max-md:mt-1">
      <div className="flex items-center justify-between gap-4 text-sub">
        <span className="font-semibold text-white/85">收录进度</span>
        <span className="tnum shrink-0 text-white/55">
          {imported} / {total} {unitLabel}
        </span>
      </div>
      <div
        role="img"
        aria-label={
          `共 ${total} ${unitLabel}，已入库 ${imported}` +
          (upgrading > 0 ? `（其中洗版中 ${upgrading}）` : "") +
          `，下载中 ${inPipeline}，缺失 ${wanted}`
        }
        className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-white/[0.12]"
      >
        <div
          className="bg-[var(--ok)]"
          style={{ width: `${(importedSettled / denom) * 100}%` }}
        />
        <div
          className="bg-[#2dd4bf]"
          style={{ width: `${(upgrading / denom) * 100}%` }}
        />
        <div
          className="bg-[#6aa7ff]"
          style={{ width: `${(inPipeline / denom) * 100}%` }}
        />
      </div>
      <div className="tnum mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-caption text-white/45">
        <ProgressLegend color="var(--ok)" label={`已入库 ${importedSettled}`} />
        {upgrading > 0 && <ProgressLegend color="#2dd4bf" label={`洗版中 ${upgrading}`} />}
        <ProgressLegend color="var(--info)" label={`下载中 ${inPipeline}`} />
        <ProgressLegend color="rgba(255,255,255,0.2)" label={`缺失 ${wanted}`} />
      </div>
    </div>
  );
}

function ProgressLegend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="size-1.5 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}

/**
 * 工单 → 此刻仍未解决的失败活动（入库失败 / 投递失败）。
 *
 * 判据是「该工单最新一条活动仍是失败」而不是比时间戳：任何后续进展
 * （重投递 / 下载完成 / 入库成功）都会给同一工单写一条新活动，最新那条
 * 一旦不是失败，说明问题已经翻篇。搜索轮次是订阅级活动（wanted_item_id
 * 为空），不会挤掉这里的判断。活动接口只取最近 100 条，更久远的失败落窗
 * 之外——那种陈年记录本来也该去排查记录里翻。
 */
function pendingFailures(
  activities: SubscriptionActivity[],
): Map<number, SubscriptionActivity> {
  const failures = new Map<number, SubscriptionActivity>();
  const seen = new Set<number>();
  // 接口按时间倒序，每个工单第一次出现的就是它最新的一条
  for (const a of activities) {
    if (a.wanted_item_id === null || seen.has(a.wanted_item_id)) continue;
    seen.add(a.wanted_item_id);
    if (a.type === "import_failed" || a.type === "dispatch_failed") {
      failures.set(a.wanted_item_id, a);
    }
  }
  return failures;
}

/**
 * 搜索轮次摘要行：SEARCHED 活动是订阅级的一轮（一轮服务所有缺口，天然拆
 * 不到集），不值得为它养一个标签页——压成一行「最近一轮 + 关键数字 +
 * 下轮时间」。payload 有结构化字段时用数字组，失败轮或老记录退化为完整
 * 句子。没有任何搜索记录时整行不渲染（如纯追新未到期）。
 *
 * 刻意只留最近一轮：翻历史轮次答不出任何可执行的问题——「搜过几次还没
 * 命中」链上的搜索站已经写着（次数 + 最近一次被拒原因），要逐条回放去底部
 * 的排查记录，不必在页面最显眼处再挂一份折叠副本。
 */
function SearchRoundBar({
  activities,
  wanted,
}: {
  activities: SubscriptionActivity[];
  wanted: WantedItem[];
}) {
  const latest = activities.find((a) => a.type === "searched");
  if (!latest) return null;

  const p = latest.payload as Record<string, unknown>;
  const hasStats = typeof p.results === "number" && !p.failed;
  // 下一轮 = 未满足工单里最近的到期时刻（后端退避曲线排出来的）
  const nextDue = wanted
    .filter((w) => w.status === "wanted" && w.next_search_at)
    .map((w) => w.next_search_at as string)
    .sort()[0];

  return (
    <div className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] backdrop-blur-xl">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
        <span
          className="size-1.5 shrink-0 rounded-full bg-[var(--info)]"
          style={{ boxShadow: "0 0 0 3px color-mix(in oklab, var(--info) 14%, transparent)" }}
        />
        <span className="text-sub font-medium text-white/85">
          {formatRelativeTime(latest.created_at)}搜了一轮
        </span>
        <span className="tnum basis-full text-caption leading-5 text-[var(--text-faint)]">
          {hasStats ? (
            <>
              {String(p.results)} 结果 · 命中 {String(p.identity_hits ?? 0)} · 拒绝{" "}
              {String(p.rejected ?? 0)} · 投递 {String(p.dispatched_units ?? 0)} 个单元
              {nextDue ? ` · 下轮 ${formatDateTime(nextDue)}` : ""}
            </>
          ) : (
            latest.message
          )}
        </span>
      </div>
    </div>
  );
}

/**
 * 排查记录：全量活动的折叠区，默认收起。主视图只讲结论（链上的卡点、
 * 失败站与注解），这里保住「按时间顺序回放系统每个动作」的兜底能力——
 * 生命周期变更、换源、洗版证伪等订阅级事件也只在这里完整可见。
 *
 * 标题自带用途说明：日常这块不该被点开，只有「链上看不出为什么」时才下钻，
 * 叫「活动流水」用户根本不知道什么时候该看它。
 */
function ActivityLogSection({ activities }: { activities: SubscriptionActivity[] }) {
  const [open, setOpen] = useState(false);
  if (activities.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] backdrop-blur-xl">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sub font-semibold text-white/70 transition-colors hover:bg-white/[0.035]"
      >
        排查记录
        <span className="font-normal text-caption text-[var(--text-faint)] max-md:hidden">
          按时间回放系统做过的每一步
        </span>
        <span className="tnum font-normal text-[var(--text-faint)]">{activities.length}</span>
        <ChevronDownIcon
          className={`ml-auto size-4 shrink-0 text-[var(--text-faint)] transition-transform ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>
      {open && (
        <div className="border-t border-white/[0.06]">
          <ActivityTimeline activities={activities} bare />
        </div>
      )}
    </div>
  );
}

/** 活动类型 → 时间线圆点颜色：绿=成果，红=失败/拒绝，黄=暂停，蓝=常规动作。 */
function activityColor(type: SubscriptionActivity["type"]): string {
  switch (type) {
    case "grabbed":
    case "match_accepted":
    case "completed":
    case "downloaded":
    case "imported":
    case "replacement_promoted":
      return "var(--ok)";
    case "upgrade_grabbed":
    case "upgraded":
      return "#2dd4bf"; // 洗版专属青色，与「洗版中」徽标/进度条分段同源
    case "match_rejected":
    case "dispatch_failed":
    case "import_failed":
      return "var(--danger)";
    case "paused":
    case "download_stalled":
    case "upgrade_verify_failed":
      return "var(--warn)";
    default:
      return "var(--info)";
  }
}

/**
 * 活动时间线（全宽竖轨）：圆点定性（颜色）+ 中文流水句 + 相对时间。
 * message 由后端写入时渲染成完整句子，前端不做模板拼接。
 */
function ActivityTimeline({
  activities,
  bare = false,
}: {
  activities: SubscriptionActivity[];
  /** 已被外层卡片（排查记录折叠区）包裹时不再画自己的边框底色 */
  bare?: boolean;
}) {
  if (activities.length === 0) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-5 text-sub leading-6 text-[var(--text-muted)] backdrop-blur-xl">
        暂无活动记录。系统开始搜索、匹配或投递后，每个动作都会记录在这里。
      </p>
    );
  }
  return (
    <div
      className={
        bare
          ? "px-6 py-5 max-md:px-4"
          : "rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] px-6 py-5 backdrop-blur-xl"
      }
    >
      <ol>
        {activities.map((a, i) => {
          const last = i === activities.length - 1;
          const color = activityColor(a.type);
          return (
            <li key={a.id} className="flex gap-4">
              {/* 竖轨：圆点 + 连接线（末条不画线） */}
              <div className="flex flex-col items-center">
                <span
                  className="mt-[7px] size-2 shrink-0 rounded-full"
                  style={{ backgroundColor: color, boxShadow: `0 0 8px color-mix(in oklab, ${color} 33%, transparent)` }}
                />
                {!last && <span className="mt-1.5 w-px flex-1 bg-white/[0.08]" />}
              </div>
              <div
                className={`flex min-w-0 flex-1 items-baseline gap-5 ${last ? "" : "pb-5"}`}
              >
                <p className="min-w-0 flex-1 text-ui leading-6 text-white/85">{a.message}</p>
                <span
                  className="tnum shrink-0 text-caption text-[var(--text-faint)]"
                  title={formatDateTime(a.created_at)}
                >
                  {formatRelativeTime(a.created_at)}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * 默认展开哪几季：最新一季 ∪ 有在途工单的季。
 *
 * 只展开最新一季不够——回补老剧时在途的那一集完全可能落在第 3 季，
 * 而最新季一潭死水。用户来这页问的是「现在卡在哪」，把正在下载的那一季
 * 折起来恰恰答错了问题。在途口径复用本文件顶部 hasInFlight 的同一条
 * （grabbed / downloaded），不含洗版中：洗版是长期后台过程、可能横跨多季，
 * 算进来等于全展开，折叠就白做了。
 *
 * 「最新一季」取季号最大的正季，只有特别篇时才落到 0——与海报卡的
 * subscriptionCollectionMeta（lib/subscription-ui.ts）同一口径。
 */
function defaultOpenSeasons(wanted: WantedItem[]): Set<number> {
  const open = new Set<number>();
  const regular = wanted.filter((w) => w.season_number > 0);
  const pool = regular.length > 0 ? regular : wanted;
  if (pool.length > 0) {
    open.add(Math.max(...pool.map((w) => w.season_number)));
  }
  for (const w of wanted) {
    if (w.status === "grabbed" || w.status === "downloaded") open.add(w.season_number);
  }
  return open;
}

/**
 * 追踪项按季分组展开；电影是单项的退化形态。
 *
 * 多季订阅（回补整部剧很常见，全 8 季 = 70 多行）默认只展开
 * defaultOpenSeasons 选中的季，其余折成季头行那条本来就有的摘要
 * （N/M 计数 + 洗版 N + 标注片源）。折叠而不是「只显示一季」：
 * 收起的每一行仍然回答着「这季还缺不缺」，换季选择器（下拉/胶囊）会把
 * 这个信息藏掉，回补老剧的用户恰恰最需要它。季序改为倒序，最新季在最上面。
 */
function WantedBreakdown({
  wanted,
  isMovie,
  downloads,
  failures,
  mediaItemId,
  canAnnotate,
  onAnnotated,
}: {
  wanted: WantedItem[];
  isMovie: boolean;
  /** info_hash → 实时下载快照（无在途工单时为空 Map） */
  downloads: Map<string, SubscriptionDownload>;
  /** 工单 id → 仍未解决的失败活动（pendingFailures） */
  failures: Map<number, SubscriptionActivity>;
  mediaItemId: number;
  /** 片源标注是库数据纠错（管理员动作），与后端 require_admin 对齐 */
  canAnnotate: boolean;
  /** 片源标注成功后回调（父组件重跑体检 + 刷新 + toast） */
  onAnnotated: (message: string) => void | Promise<void>;
}) {
  // 「无法确认档位」季的标注弹窗（docs/design/media-source-annotation.md §5.1）
  const [annotateSeason, setAnnotateSeason] = useState<number | null>(null);
  // 惰性初始化只在挂载时算一次：详情每 30s 静默刷新一遍，若跟着 wanted 重算，
  // 用户手动展开的季会被刷回去。代价是自动续订新增的季不会自动展开——
  // 折叠行本身就写着进度，不算信息丢失。
  const [openSeasons, setOpenSeasons] = useState(() => defaultOpenSeasons(wanted));
  const toggleSeason = (season: number) => {
    setOpenSeasons((prev) => {
      const next = new Set(prev);
      if (next.has(season)) next.delete(season);
      else next.add(season);
      return next;
    });
  };
  // 展开的集（里程碑链）：同一时刻只开一条——链是「细看一集」的动作，
  // 多条同开只会把页面重新拉长
  const [openWanted, setOpenWanted] = useState<number | null>(null);
  if (wanted.length === 0) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-5 text-sub leading-6 text-[var(--text-muted)] backdrop-blur-xl">
        当前没有追踪项。开启「自动续订」后，新集播出会自动加入。
      </p>
    );
  }

  const seasons = new Map<number, WantedItem[]>();
  for (const w of wanted) {
    const list = seasons.get(w.season_number) ?? [];
    list.push(w);
    seasons.set(w.season_number, list);
  }
  const annotatable = canAnnotate ? seasonsWithIndeterminate(wanted) : new Set<number>();
  // 只有一组时折叠没有意义（电影、单季剧），维持原来的常展开形态
  const collapsible = !isMovie && seasons.size > 1;

  return (
    <div className="space-y-4">
      {[...seasons.entries()]
        // 倒序：最新一季在最上面，也就是默认展开的那一季，进页面不用先往下滚
        .sort(([a], [b]) => b - a)
        .map(([season, items]) => {
          const open = !collapsible || openSeasons.has(season);
          const arranged = items.filter((w) => w.status !== "wanted").length;
          return (
            <div
              key={season}
              className="overflow-hidden rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] backdrop-blur-xl"
            >
              {(!isMovie || annotatable.has(season)) && (
                /* 折叠热区用 role="button" 的 div 而不是 <button>：季头行里还有
                   「标注片源」按钮，button 套 button 是非法嵌套。与站点配置行
                   （site-config-section.tsx）同一套写法。 */
                <div
                  role={collapsible ? "button" : undefined}
                  tabIndex={collapsible ? 0 : undefined}
                  aria-expanded={collapsible ? open : undefined}
                  onClick={collapsible ? () => toggleSeason(season) : undefined}
                  onKeyDown={
                    collapsible
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            toggleSeason(season);
                          }
                        }
                      : undefined
                  }
                  className={`flex items-center gap-2 px-5 py-2.5 text-sub font-semibold text-white/80 ${
                    open ? "border-b border-white/[0.06]" : ""
                  } ${
                    collapsible
                      ? "cursor-pointer transition-colors hover:bg-white/[0.035] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-white/25"
                      : ""
                  }`}
                >
                  {collapsible && (
                    <ChevronDownIcon
                      className={`size-4 shrink-0 text-[var(--text-faint)] transition-transform ${
                        open ? "rotate-180" : ""
                      }`}
                    />
                  )}
                  <span className="min-w-0 flex-1 truncate">
                    {isMovie ? "正片" : season === 0 ? "特别篇" : `第 ${season} 季`}
                    {/* 计数不带「已安排」标签：集行的状态胶囊已让语义自明，
                        短形态在窄屏不与洗版数、标注按钮抢宽度 */}
                    {!isMovie && (
                      <span
                        className="tnum ml-2 font-normal text-[var(--text-faint)]"
                        title={`${arranged}/${items.length} 已安排`}
                      >
                        {arranged}/{items.length}
                      </span>
                    )}
                    {items.some((w) => w.upgrade?.active) && (
                      <span className="tnum ml-2 font-normal text-[#2dd4bf]/80">
                        洗版 {items.filter((w) => w.upgrade?.active).length}
                      </span>
                    )}
                  </span>
                  {/* 收起态的迷你进度条：一列折叠行里，光看数字扫不出哪季有缺口。
                      刻意与同行 N/M 计数取同一个比值——旁边的条和数字讲两件事，
                      用户只会更糊涂 */}
                  {collapsible && !open && (
                    <span className="h-1 w-12 shrink-0 overflow-hidden rounded-full bg-white/[0.1]">
                      <span
                        className="block h-full rounded-full"
                        style={{
                          width: `${Math.round((arranged / items.length) * 100)}%`,
                          backgroundColor:
                            arranged === items.length ? "var(--ok)" : "var(--warn)",
                        }}
                      />
                    </span>
                  )}
                  {annotatable.has(season) && (
                    <button
                      type="button"
                      onClick={(e) => {
                        // 整行是折叠热区，标注按钮不能顺带把这一季折起来
                        e.stopPropagation();
                        setAnnotateSeason(season);
                      }}
                      className="shrink-0 rounded-md border border-white/[0.12] px-2 py-0.5 text-caption font-medium text-white/75 transition hover:bg-white/[0.07] hover:text-white"
                    >
                      标注片源
                    </button>
                  )}
                </div>
              )}
              {open && (
                <ul className="divide-y divide-white/[0.05]">
                  {items.map((w) => (
                    <WantedRow
                      key={w.id}
                      wanted={w}
                      isMovie={isMovie}
                      download={w.info_hash ? downloads.get(w.info_hash) : undefined}
                      failure={failures.get(w.id)}
                      expanded={openWanted === w.id}
                      onToggle={() =>
                        setOpenWanted((cur) => (cur === w.id ? null : w.id))
                      }
                    />
                  ))}
                </ul>
              )}
            </div>
          );
        })}

      {annotateSeason !== null && (
        <MediaSourceAnnotationDialog
          mediaItemId={mediaItemId}
          seasonNumber={annotateSeason}
          isMovie={isMovie}
          onClose={() => setAnnotateSeason(null)}
          onApplied={async (message) => {
            setAnnotateSeason(null);
            await onAnnotated(message);
          }}
        />
      )}
    </div>
  );
}

/**
 * wantedPresentation 长词 → 状态胶囊三字短名。整套词表恰好能全部收敛到
 * 三字（配合微型链固定占宽 → 所有胶囊等宽，右轨对成一列状态列）；
 * 丢掉的字由行说明与展开链补全（「已提交下载器」），胶囊只承担短名。
 */
const SHORT_STATUS: Record<string, string> = {
  入库失败: "失败",
  已提交下载: "已提交",
  排队搜索: "排队中",
  预测窗口: "预测中",
};

/** 里程碑链的一站。state：done=历史（中性）/ now=卡点（状态色）/ todo=未到 */
interface Milestone {
  lab: string;
  /** fail = 卡在这一站且不会自愈（需要用户处理），与 now 同色同亮点、走红色 */
  state: "done" | "now" | "todo" | "fail";
  /** 右侧时间列：done 站的发生时刻、搜索站的次数等辅助短语；可为空 */
  time: string;
  det: string;
  /** 红色补充行：最近一次候选被拒原因 / 投递失败原因 / 入库失败原因 */
  why?: string;
  /** 弱化补充行：投递种子名 / 资源发布→提交时间链 */
  src?: string[];
}

/**
 * 工单 → 里程碑链：播出 → 搜索 → 投递 → 下载 → 入库 →（洗版，规则组配了
 * 目标才有）。六站时间戳全部来自工单自身字段，第一个非 done 的站就是这集
 * 卡住的地方；now 站颜色恒等于胶囊色（wantedPresentation 同源），胶囊亮点、
 * 胶囊文字、链上卡点三处同色互认。
 *
 * 电影哨兵 (0,0) 的第一站叫「上映」且不读 air_date——上映日刻意不写进
 * air_date（见 WantedItem 模型注释），用 next_search_at 是否可调度判断定档。
 */
function milestonesOf(
  w: WantedItem,
  isMovie: boolean,
  live: SubscriptionDownload | undefined,
  failure?: SubscriptionActivity,
): Milestone[] {
  const M: Milestone[] = [];
  const now = new Date();

  // 1. 播出 / 上映——语义是「定档站」：定档即 done（det 带日期，未来场次也
  //    如实写「X 播出」），未定档才是卡点。待播出 / 预测窗口的等待发生在
  //    搜索侧（wantedPresentation 的措辞也如此），卡点归搜索站，否则会出现
  //    胶囊叫「预测中」而亮点指着播出站的名实错位；资源先于播出流出的
  //    已投递单元也不会被误标「已播出」。
  if (isMovie) {
    const undated = w.status === "wanted" && w.next_search_at === null;
    M.push(
      undated
        ? { lab: "上映", state: "now", time: "", det: "上映日期未公布，定档后自动排队" }
        : { lab: "上映", state: "done", time: "", det: "已定档或已上映" },
    );
  } else if (!w.air_date) {
    M.push({ lab: "播出", state: "now", time: "", det: "播出日期未公布，定档后自动排队" });
  } else {
    M.push({
      lab: "播出",
      state: "done",
      time: w.air_date,
      det: new Date(w.air_date) > now ? `${w.air_date} 播出` : "已播出",
    });
  }
  // 未定档 = 不可调度，搜索站保持 todo；已定档未播出的等待语义交给搜索站
  const preAir = M[0].state === "now";

  // 2. 搜索：卡在这里时直接复用 wantedPresentation 的调度文案
  //   （排队 / 冷却 / 预测窗口 / 待搜索的精确措辞单源维护，不重写一遍）
  if (w.status !== "wanted") {
    M.push({
      lab: "搜索",
      state: "done",
      time: w.last_search_at ? formatRelativeTime(w.last_search_at) : "",
      det: w.search_attempts > 0 ? `搜索 ${w.search_attempts} 次后命中` : "被动匹配命中",
    });
  } else if (preAir) {
    M.push({ lab: "搜索", state: "todo", time: "", det: "定档后进入搜索队列" });
  } else {
    M.push({
      lab: "搜索",
      state: "now",
      time: w.search_attempts > 0 ? `已搜 ${w.search_attempts} 次` : "",
      det: wantedPresentation(w).note,
      why: w.last_reject_reason ? `最近一次被拒：${w.last_reject_reason}` : undefined,
    });
  }

  // 3. 投递
  if (w.grabbed_at) {
    const src = [
      w.grab_title,
      resourceTimingNote(w.resource_timing, false),
    ].filter((s): s is string => !!s);
    M.push({
      lab: "投递",
      state: "done",
      time: formatRelativeTime(w.grabbed_at),
      det: "已提交下载器",
      src: src.length ? src : undefined,
    });
  } else {
    // 重开的工单（缺失重下）grabbed_at 已清但上次投递的时间链还在：
    // 保留「上次 · 站点：耗时」注解，与旧版行内第二行的信息对齐
    const lastTiming = resourceTimingNote(w.resource_timing, true);
    // 投递失败会退回队列自动重试（卡点仍在搜索站），所以不占用失败站，
    // 只借搜索站同款的红色注解把原因说清——否则链上写着「尚未找到资源」，
    // 而真相是资源找到了、提交下载器时出错
    const dispatchFailure = failure?.type === "dispatch_failed" ? failure : undefined;
    M.push({
      lab: "投递",
      state: "todo",
      time: "",
      det: dispatchFailure ? "上次投递未成功，已退回队列" : "尚未找到符合规则组的资源",
      why: dispatchFailure?.message,
      src: lastTiming ? [lastTiming] : undefined,
    });
  }

  // 4. 下载
  // 里程碑链是顺序的：入库发生过就说明下载必然已经完成。后端把 downloaded
  // 这一档折叠掉了（文件落盘即整理入库，``wanted_item.downloaded_at`` 至今
  // 没有任何写入方），只看它会让这一格在入库之后仍然停在"进行中"，与下一格
  // 的"已整理入库"自相矛盾——用户据此以为没入库
  const downloadedAt = w.downloaded_at ?? w.imported_at;
  if (downloadedAt) {
    M.push({
      lab: "下载",
      state: "done",
      time: formatRelativeTime(downloadedAt),
      det: "全部落盘",
    });
  } else if (w.grabbed_at) {
    M.push({
      lab: "下载",
      state: "now",
      time: "进行中",
      det: live ? downloadNote(live) : "已提交下载器，等待进度汇报",
    });
  } else {
    M.push({ lab: "下载", state: "todo", time: "", det: "等待投递完成" });
  }

  // 5. 入库
  if (w.imported_at) {
    M.push({
      lab: "入库",
      state: "done",
      time: formatRelativeTime(w.imported_at),
      det: w.upgrade ? `已整理入库 · ${w.upgrade.current_label}` : "已整理入库",
    });
  } else if (failure?.type === "import_failed") {
    // 文件已落盘却整理不进库：不修配置就永远停在这里，如实标红并把
    // 后端写好的中文原因（含修复指引）原样带出来，别让用户干等
    M.push({
      lab: "入库",
      state: "fail",
      time: "",
      det: "下载完成了，但 movieclaw 无法把文件整理入库",
      why: failure.message,
    });
  } else if (w.downloaded_at) {
    M.push({ lab: "入库", state: "now", time: "", det: "下载完成，等待整理入库" });
  } else {
    M.push({ lab: "入库", state: "todo", time: "", det: "下载完成后自动整理" });
  }

  // 6. 洗版（规则组配了洗版目标才出现这一站）
  if (w.upgrade) {
    if (w.upgrade.active) {
      M.push({
        lab: "洗版",
        state: "now",
        time: w.upgrade.search_attempts > 0 ? `已搜 ${w.upgrade.search_attempts} 次` : "",
        det: `当前 ${w.upgrade.current_label} → 目标 ${w.upgrade.target_label}`,
      });
    } else if (w.upgrade.indeterminate) {
      M.push({
        lab: "洗版",
        state: "now",
        time: "",
        det: `${w.upgrade.current_label} · 无法确认是否低于洗版目标，不自动洗；可在季标题「标注片源」或手动选种替换`,
      });
    } else {
      M.push({
        lab: "洗版",
        state: "done",
        time: "",
        det: `当前 ${w.upgrade.current_label}`,
      });
    }
  }
  return M;
}

/** 卡点 = 链上第一个非 done 的站；全 done（收齐且无洗版任务）时亮终点站。 */
function stuckIndex(chain: Milestone[]): number {
  const i = chain.findIndex((m) => m.state !== "done");
  return i < 0 ? chain.length - 1 : i;
}

/**
 * 展开的里程碑链：竖轨节点 + 站名 + 时间列 + 说明。当前站零额外几何
 * （色块/色条都是对链语法的重复）：发光节点是链原生的选中指示器，站名染成
 * 同一状态色作文字锚点——一列中性站名里唯一有色的那个就是卡点。
 */
function MilestoneChain({ chain, color }: { chain: Milestone[]; color: string }) {
  const stuck = stuckIndex(chain);
  return (
    /* 左缩进 76px = 行内边距 20 + 集号列 44 + 间距 12：链与行说明文字同列起步 */
    <ol className="bg-white/[0.05] py-3 pl-[76px] pr-5 max-md:px-4">
      {chain.map((m, i) => {
        // fail 与 now 同款高亮：卡点只有一个，失败站就是那个卡点，
        // 颜色由外层胶囊色统一给（入库失败时整行已是红色）
        const isNow = m.state === "now" || m.state === "fail";
        return (
          <li key={m.lab} className="flex gap-3">
            <span className="flex flex-col items-center">
              <span
                className={`shrink-0 rounded-full ${
                  isNow ? "mt-1.5 size-2.5" : "mt-[7px] size-[9px]"
                } ${m.state === "done" ? "bg-white/30" : "border-[1.5px] border-white/[0.15]"}`}
                style={
                  isNow
                    ? {
                        borderColor: color,
                        borderWidth: 2.5,
                        boxShadow: `0 0 9px color-mix(in oklab, ${color} 40%, transparent)`,
                      }
                    : undefined
                }
              />
              {i < chain.length - 1 && (
                /* 轨线分段：走过的亮、没走的暗——链断在哪一眼可见 */
                <span
                  className={`mt-1 w-[1.5px] flex-1 rounded-full ${
                    i < stuck ? "bg-white/[0.16]" : "bg-white/[0.07]"
                  }`}
                />
              )}
            </span>
            {/* 行距收口在倒数第一站：last: 变体在这里不可用——本 div 恒为
                li 的最后子元素，last:pb-1 会让每一站的行距都塌掉 */}
            <div
              className={`flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 ${
                i === chain.length - 1 ? "pb-1" : "pb-3.5"
              }`}
            >
              <span
                className={`text-sub ${
                  isNow
                    ? "font-semibold"
                    : m.state === "done"
                      ? "font-medium text-white/65"
                      : "font-medium text-[var(--text-faint)]"
                }`}
                style={isNow ? { color } : undefined}
              >
                {m.lab}
              </span>
              {m.time && (
                <span className="tnum ml-auto shrink-0 text-caption text-[var(--text-faint)]">
                  {m.time}
                </span>
              )}
              <span
                className={`tnum basis-full text-sub leading-6 ${
                  m.state === "done"
                    ? "text-[var(--text-faint)]"
                    : m.state === "todo"
                      ? "text-white/25"
                      : "text-white/80"
                }`}
              >
                {m.det}
              </span>
              {m.why && (
                <span className="basis-full text-caption leading-5 text-[#ff8a8a]/90">
                  {m.why}
                </span>
              )}
              {m.src?.map((s) => (
                <span
                  key={s}
                  className="basis-full truncate text-caption leading-5 text-[var(--text-faint)]"
                >
                  {s}
                </span>
              ))}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 单个追踪项：一行「集号 + 说明 + 状态胶囊」，点开展开该集的里程碑链
 * （电影只有「正片」一条，不折叠，链常展开）。
 * 胶囊 = 微型链（亮点指卡在第几站）+ 三字短名（那一站的细分状态名）——
 * 同一事实的位置与名字物理相邻，且与展开链共用一套语法与颜色。
 * 说明文案与后端调度语义一一对应（补旧排队 / 追新被动匹配 / 未定档不可调度）。
 */
function WantedRow({
  wanted: w,
  isMovie,
  download,
  failure,
  expanded,
  onToggle,
}: {
  wanted: WantedItem;
  isMovie: boolean;
  /** 该工单锚定种子的实时下载快照（仅在途工单有，5s 轮询更新） */
  download?: SubscriptionDownload;
  /** 该工单仍未解决的失败活动（pendingFailures），没有则一切正常 */
  failure?: SubscriptionActivity;
  expanded: boolean;
  onToggle: () => void;
}) {
  // 入库失败是死局（多为下载器路径映射缺失/卷未挂载），必须让胶囊自己变红：
  // 工单状态仍是「已下载」，不覆盖的话这一行看起来与正常等待入库毫无区别，
  // 用户扫列表时根本不会点开去看链。投递失败会自动退回队列重试，不算卡住，
  // 只在链上留一条注解（见 milestonesOf）
  const stuckOnImport = failure?.type === "import_failed" ? failure : undefined;
  const { label, color, note: statusNote } = stuckOnImport
    ? { label: "入库失败", color: "var(--danger)", note: stuckOnImport.message }
    : wantedPresentation(w);
  // 已提交下载且拿到了实时快照：说明行升级为进度行（进度条 + 速度/ETA）。
  // 入库失败时不要这条进度行——它只会重复「已下载完成」，把失败原因挤掉
  const live =
    !stuckOnImport && (w.status === "grabbed" || w.status === "downloaded")
      ? download
      : undefined;
  const chain = milestonesOf(w, isMovie, live, failure);
  const lit = stuckIndex(chain);
  const complete = chain.every((m) => m.state === "done");
  // 电影只有「正片」一条工单，折叠没有任何可选性——链常展开、去掉箭头与点击热区，
  // 与季头行「只有一组时不折叠」是同一条规则
  const collapsible = !isMovie;
  const open = !collapsible || expanded;
  // 行头在两种形态下共用同一套排版，只有交互态（指针/悬停/展开底色）随折叠能力增减
  const headerClass = `block w-full px-5 py-2.5 text-left max-md:px-4 ${
    collapsible
      ? `cursor-pointer transition-colors hover:bg-white/[0.035] ${
          expanded ? "bg-white/[0.05]" : ""
        }`
      : ""
  }`;
  const header = (
    <>
      {/* 桌面单行：集号 | 说明(截断) | 胶囊 | 箭头；窄屏两行——定长元素留
          第一行（胶囊 ml-auto 靠右），说明整宽折到第二行（basis-full），
          截断只剩半个日期的信息量归零问题就此消失 */}
      {/* max-md:flex-wrap 是窄屏两行布局的开关：没有它 basis-full 不换行、
          说明文案被压成一条缝。桌面保持 nowrap 单行 */}
      <span className="flex items-center gap-x-3 gap-y-1 max-md:flex-wrap">
        <span className="tnum w-11 shrink-0 text-sub font-semibold text-white/90">
          {isMovie ? "正片" : `E${String(w.episode_number).padStart(2, "0")}`}
        </span>
        <span className="tnum min-w-0 flex-1 truncate text-sub leading-5 text-[var(--text-muted)] max-md:order-last max-md:basis-full max-md:whitespace-normal max-md:line-clamp-2">
          {live ? downloadNote(live) : statusNote}
        </span>
        <span
          className="flex shrink-0 items-center gap-[7px] rounded-full px-2.5 py-[3px] text-micro font-semibold max-md:ml-auto"
          style={{
            backgroundColor: `color-mix(in oklab, ${color} 14%, transparent)`,
            color,
          }}
          title={`第 ${lit + 1}/${chain.length} 站：${chain[lit].lab}${complete ? "（已收齐）" : ""}`}
        >
          {/* 微型链固定占最长链（6 站）的宽度：与三字短名合力让胶囊等宽 */}
          <span className="flex w-[42px] shrink-0 items-center gap-[3px]">
            {chain.map((m, i) => (
              <span
                key={m.lab}
                className="size-[4.5px] shrink-0 rounded-full"
                style={{
                  backgroundColor:
                    i === lit
                      ? color
                      : m.state === "done"
                        ? "rgba(255,255,255,.38)"
                        : "rgba(255,255,255,.14)",
                  boxShadow:
                    i === lit
                      ? `0 0 5px color-mix(in oklab, ${color} 60%, transparent)`
                      : undefined,
                }}
              />
            ))}
          </span>
          {SHORT_STATUS[label] ?? label}
        </span>
        {collapsible && (
          <ChevronDownIcon
            className={`size-3.5 shrink-0 text-white/30 transition-transform ${
              expanded ? "rotate-180 text-[var(--text-muted)]" : ""
            }`}
          />
        )}
      </span>
      {live && live.progress != null && live.state !== "missing" && (
        <span
          className="mt-2 block h-1 overflow-hidden rounded-full bg-white/[0.08]"
          role="progressbar"
          aria-valuenow={Math.round(live.progress * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <span
            className="block h-full rounded-full bg-[var(--ok)] transition-[width] duration-700"
            style={{ width: `${Math.min(100, Math.max(1, live.progress * 100))}%` }}
          />
        </span>
      )}
    </>
  );
  return (
    <li>
      {collapsible ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={onToggle}
          className={headerClass}
        >
          {header}
        </button>
      ) : (
        <div className={headerClass}>{header}</div>
      )}
      {open && <MilestoneChain chain={chain} color={color} />}
    </li>
  );
}

/** 实时快照 → 一行进度说明。词表见 SubscriptionDownload.state。 */
function downloadNote(d: SubscriptionDownload): string {
  if (d.state === "missing") {
    return "种子已不在下载器中（可能被手动删除），稍后自动重新寻找资源";
  }
  const pct = d.progress != null ? `${Math.floor(d.progress * 100)}%` : "";
  if (d.state === "completed") return "已下载完成，等待整理入库";
  if (d.state === "paused") return `${pct} · 已在下载器中暂停`;
  if (d.state === "error") return `${pct} · 下载器报告任务出错`;
  if (d.state === "stalled") return `${pct} · 等待连接做种`;
  // downloading / unknown：速度与 ETA 有则展示
  const parts = [pct];
  if (d.dlspeed_bytes != null && d.dlspeed_bytes > 0) {
    parts.push(`${formatBytes(d.dlspeed_bytes)}/s`);
  }
  if (d.eta_seconds != null) parts.push(`剩余约 ${formatDuration(d.eta_seconds)}`);
  if (d.size_bytes != null && parts.length === 1) parts.push(formatBytes(d.size_bytes));
  return parts.filter(Boolean).join(" · ");
}

/** 资源时间链 → 用户能直接感知的“隔了多久才拉到”。 */
function resourceTimingNote(timing: ResourceTiming | null, previous: boolean): string | null {
  if (!timing || !shouldShowResourceTiming(timing.publish_to_submit_seconds)) return null;
  const delay = (seconds: number) =>
    seconds < 60 ? "不到 1 分钟" : formatDuration(seconds);
  const prefix = `${previous ? "上次 · " : ""}${timing.dry_run ? "模拟 · " : ""}${timing.site_id}：`;
  if (
    timing.publish_to_seen_seconds != null &&
    timing.seen_to_submit_seconds != null &&
    timing.publish_to_submit_seconds != null
  ) {
    return (
      `${prefix}资源发布后 ${delay(timing.publish_to_seen_seconds)}进入索引，` +
      `${delay(timing.seen_to_submit_seconds)}后提交下载器` +
      `（总耗时 ${delay(timing.publish_to_submit_seconds)}）`
    );
  }
  if (timing.publish_to_submit_seconds != null) {
    return `${prefix}资源发布后 ${delay(timing.publish_to_submit_seconds)}提交下载器`;
  }
  if (timing.seen_to_submit_seconds != null) {
    return `${prefix}进入索引后 ${delay(timing.seen_to_submit_seconds)}提交下载器`;
  }
  return null;
}

function wantedPresentation(w: WantedItem): { label: string; color: string; note: string } {
  if (w.status === "imported") {
    // 洗版中（quality-upgrade.md §8.3）：已入库但规则组的洗版目标还没达到。
    // 标签与差距文案由后端统一生成（当前档 → 目标档），前端零拼接
    if (w.upgrade?.active) {
      // 搜索次数不进说明行——行尾已有统一的「已搜索 n 次」元素，重复即噪音
      return {
        label: "洗版中",
        color: "#2dd4bf",
        note: `当前 ${w.upgrade.current_label} → 目标 ${w.upgrade.target_label}`,
      };
    }
    if (w.upgrade?.indeterminate) {
      // 无法确认档位（§13.8）：不参与自动洗版——如实说明并给出人工出路，
      // 否则这类单元与档位健康的单元毫无区别，用户以为它在洗版盘子里
      return {
        label: "已入库",
        color: "var(--ok)",
        note: `${w.upgrade.current_label} · 无法确认是否低于洗版目标，不自动洗；可在季标题「标注片源」，或手动选种替换`,
      };
    }
    if (w.upgrade) {
      // 规则组开了洗版但该单元已到顶/暂休：带出当前档位，让「库里是什么
      // 版本」始终可见（自然学习路径，quality-upgrade.md §8.5）
      return {
        label: "已入库",
        color: "var(--ok)",
        note: `${w.upgrade.current_label} · 入库于 ${formatDateTime(w.imported_at)}`,
      };
    }
    return { label: "已入库", color: "var(--ok)", note: `入库于 ${formatDateTime(w.imported_at)}` };
  }
  if (w.status === "downloaded") {
    return {
      label: "已下载",
      color: "var(--ok)",
      note: `完成于 ${formatDateTime(w.downloaded_at ?? w.grabbed_at)}，待整理入库`,
    };
  }
  if (w.status === "grabbed") {
    return {
      label: "已提交下载",
      color: "var(--ok)",
      note: `${formatRelativeTime(w.grabbed_at)}提交给下载器`,
    };
  }
  // status === "wanted"：按调度语义解释它此刻卡在哪。
  // 文案刻意写短：徽标已经说清「是什么状态」，这一行只补「关键的那个时间点」。
  // 一屏几十行同状态的追踪项，把机制解释重复几十遍纯属噪音，窄屏上还会被截断
  // 到只剩半个日期（iPhone 实测）——机制说明留给徽标语义本身。
  if (w.next_search_at === null) {
    return {
      label: "未定档",
      color: "#9ca3af",
      note: "上映/播出日期未公布，定档后自动排队",
    };
  }
  const due = new Date(w.next_search_at);
  const now = new Date();
  const forecast = w.release_forecast;
  if (
    w.air_date &&
    forecast?.version === 1 &&
    forecast.target_air_date === w.air_date &&
    forecast.confidence !== "volatile" &&
    forecast.sites.length > 0 &&
    new Date(forecast.window_end) >= now
  ) {
    const sites = forecast.sites
      .slice(0, 2)
      .map((site) => site.site_id)
      .join("、");
    return {
      label: "预测窗口",
      color: "var(--warn)",
      note: `预计 ${formatDateTime(forecast.window_start)} ～ ${formatDateTime(forecast.window_end)}，重点探测 ${sites}`,
    };
  }
  if (w.air_date && new Date(w.air_date) > now) {
    return {
      label: "待播出",
      color: "var(--warn)",
      note: `${w.air_date} 播出，${formatDateTime(w.next_search_at)} 起兜底搜索`,
    };
  }
  if (due <= new Date()) {
    return {
      label: "排队搜索",
      color: "var(--info)",
      note: w.last_search_at
        ? `上次搜索 ${formatRelativeTime(w.last_search_at)}`
        : "等待搜索任务执行",
    };
  }
  // 从未搜索过却排在远处 = 档期未到（电影上映+宽限、剧集播出+宽限），
  // 不是"搜过没结果"的冷却——文案分开，别让用户误以为已经白搜过。
  // 1 小时门槛用来排除"搜索本身失败"的短重试（15 分钟顺延不记 last_search_at），
  // 那种情况维持原本的冷却展示
  if (!w.last_search_at && due.getTime() - now.getTime() > 60 * 60 * 1000) {
    return {
      label: "待搜索",
      color: "#f5c451",
      note: `档期未到，${formatDateTime(w.next_search_at)} 起兜底搜索`,
    };
  }
  return {
    label: "冷却中",
    color: "var(--info)",
    note: `暂无合适资源，${formatDateTime(w.next_search_at)} 再试`,
  };
}
