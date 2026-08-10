"use client";

import Link from "next/link";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useConfirm, useToast } from "@/components/feedback";
import { ArrowLeftIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { PageNav } from "@/components/page-nav";
import { usePageTitle } from "@/lib/use-page-title";
import { PosterImage } from "@/components/poster-image";
import { specSummary } from "@/components/rule-sets-panel";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { SubscriptionAdjustDialog } from "@/components/subscription-adjust-dialog";
import {
  deleteSubscription,
  getSubscription,
  listRuleSets,
  listSubscriptionActivities,
  listSubscriptionDownloads,
  pauseSubscription,
  searchSubscriptionNow,
  updateSubscription,
  type RuleSet,
  type SubscriptionActivity,
  type SubscriptionDetail,
  type SubscriptionDownload,
  type WantedItem,
} from "@/lib/api/subscriptions";
import { formatBytes, formatDuration } from "@/lib/format";
import { cachedImageUrl } from "@/lib/image-proxy";
import {
  subscriptionProgressNote,
  subscriptionStatusMeta,
} from "@/lib/subscription-ui";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/**
 * 订阅详情分析页（/subscriptions/[id]）：订阅透明化的落点。
 *
 * 页面结构（与影片详情页同一套视觉语言）：
 *   1. Hero 氛围横幅 —— 海报重度模糊铺底产出该片专属底色（订阅接口无宽幅
 *      剧照，模糊海报是永远可用的兜底），上面放海报 / 标题 / 状态与参数徽片，
 *      底部一条「已入库 / 下载中 / 缺口」三段式进度条，操作按钮收右上；
 *   2. 标签页主体 —— 「追踪明细」与「活动记录」性质不同（一个是可变的状态
 *      快照，一个是只增的事件流水），不再左右分栏互相挤压，改为胶囊标签
 *      切换、各占全宽：
 *      - 追踪明细：按季分组的工单明细，含调度信息（排队中 / 待播出 /
 *        未定档 / 已提交下载），让「正在寻找资源」背后的每个单元都可见；
 *      - 活动记录：竖轨时间线，后端每个动作的中文流水全宽展示
 *        （创建 / 搜索 / 匹配 / 拒绝原因 / 投递 / 入库），长句不再折行成豆腐块。
 */
export function SubscriptionInspectorView({ id }: { id: number }) {
  const router = useRouter();
  const confirm = useConfirm();
  // 暂停/取消订阅会改变全站订阅状态（海报卡片的「已订阅」徽标），操作后同步刷新
  const { refresh: refreshSubscriptions } = useSubscribeEntry();
  const [detail, setDetail] = useState<SubscriptionDetail | null>(null);
  const [activities, setActivities] = useState<SubscriptionActivity[]>([]);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [switchingRule, setSwitchingRule] = useState(false);
  const [adjusting, setAdjusting] = useState(false);
  const [tab, setTab] = useState<"wanted" | "activity">("wanted");
  const toast = useToast();

  const [downloads, setDownloads] = useState<SubscriptionDownload[]>([]);

  const reload = useCallback(() => {
    Promise.all([
      getSubscription(id),
      listSubscriptionActivities(id),
      listRuleSets(),
    ])
      .then(([d, acts, rules]) => {
        setDetail(d);
        setActivities(acts);
        setRuleSets(rules);
      })
      .catch(() => setFailed(true));
  }, [id]);

  useEffect(() => {
    reload();
  }, [reload]);

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
      listSubscriptionDownloads(id)
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

  const ruleSetName = useMemo(
    () => ruleSets.find((r) => r.id === detail?.rule_set_id)?.name ?? `#${detail?.rule_set_id}`,
    [ruleSets, detail],
  );
  const qualityPolicyNote = useMemo(() => {
    const policy = detail?.quality_policy;
    if (!policy) return null;
    const locked = policy.locked ? specSummary(policy.locked) : [];
    if (locked.length > 0) return `已固定 ${locked.join(" · ")}`;
    if (policy.mode === "lock_first") return "等待首次入库锁定版本";
    const target = policy.target ? specSummary(policy.target) : [];
    if (
      detail?.media.kind === "movie" &&
      detail.progress.imported > 0 &&
      detail.progress.wanted === 0 &&
      detail.progress.upgrading === 0 &&
      detail.progress.grabbed + detail.progress.downloaded === 0
    ) {
      return `洗版已达标${target.length > 0 ? ` · ${target.join(" · ")}` : ""}`;
    }
    return `洗版目标 ${policy.target_rule_name ?? (target.join(" · ") || "指定版本")}`;
  }, [detail]);
  usePageTitle(detail?.media.title);

  // 兜底态（加载中/失败）也渲染 PageNav（片名未知，末项留空）：向外壳登记
  // 「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失、顶部闪一下。
  const fallbackTrail = [{ label: "我的订阅", href: "/subscriptions" }, { label: "" }];

  if (failed) {
    return (
      <div className="flex h-full flex-col">
        <PageNav items={fallbackTrail} />
        <div className="flex flex-1 flex-col items-center justify-center gap-4">
          <p className="text-body text-[var(--text-muted)]">未能加载该订阅，可能已被删除。</p>
          <Link href="/subscriptions" className="btn-glass px-4 py-2 text-ui font-medium">
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
        <PageNav items={fallbackTrail} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载订阅详情…
        </div>
      </div>
    );
  }

  const meta = subscriptionStatusMeta[detail.status];
  const isMovie = detail.media.kind === "movie";
  const poster = detail.media.poster_url ? cachedImageUrl(detail.media.poster_url) : null;

  const togglePause = async () => {
    setBusy(true);
    try {
      await pauseSubscription(detail.id, detail.status !== "paused");
      reload();
      refreshSubscriptions();
    } finally {
      setBusy(false);
    }
  };

  // 立即搜索：缺口跳过冷却重新排队。后端对"暂停中/没有可搜缺口"给可读错误，
  // 原样进 toast——按钮不做前置禁用判断，语义由唯一实现（服务端）说了算
  const searchNow = async () => {
    setBusy(true);
    try {
      const { reset_count } = await searchSubscriptionNow(detail.id);
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
      description: "已下载的内容不受影响。",
      confirmLabel: "取消订阅",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await deleteSubscription(detail.id);
      refreshSubscriptions();
      router.push("/subscriptions");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto px-6 pb-12 max-md:px-4">
      {/* 顶栏：返回订阅列表 + 吸顶片名（容器已有 px-6，用 -mx-6 让吸顶蒙版铺满） */}
      <PageNav
        items={[{ label: "我的订阅", href: "/subscriptions" }, { label: detail.media.title }]}
        className="-mx-6 max-md:-mx-4"
      />
      {/* —— 1. Hero 氛围横幅：模糊海报铺底 + 订阅摘要 —— */}
      <div className="relative overflow-hidden rounded-2xl bg-[#10131b] shadow-[0_24px_70px_-18px_rgba(0,0,0,0.62)] ring-1 ring-white/10">
        {poster && (
          <PosterImage
            src={poster}
            alt=""
            className="absolute inset-0 size-full scale-125 object-cover blur-3xl brightness-[0.55] saturate-[1.2]"
          />
        )}
        {/* 左深右浅的横向渐变：左侧文字区压暗保可读，右侧透出氛围色 */}
        <div className="absolute inset-0 bg-gradient-to-r from-[rgba(7,9,14,0.82)] via-[rgba(7,9,14,0.58)] to-[rgba(7,9,14,0.36)]" />

        <div className="relative z-10 flex flex-wrap items-start gap-5 p-6 max-md:gap-4 max-md:p-4">
          {poster && (
            <Link
              href={`/media/${detail.media.kind}/${detail.media.tmdb_id}`}
              className="block w-[104px] shrink-0 overflow-hidden rounded-lg shadow-[0_16px_40px_rgba(0,0,0,0.5)] ring-1 ring-white/15"
            >
              <PosterImage
                src={poster}
                alt={`${detail.media.title} 海报`}
                className="aspect-[2/3] w-full object-cover"
              />
            </Link>
          )}

          <div className="min-w-0 flex-1">
            <p className="text-caption font-semibold tracking-[0.22em] text-[var(--accent-2)]">
              {isMovie ? "电影订阅" : "剧集订阅"}
            </p>
            <h1 className="mt-1.5 flex flex-wrap items-baseline gap-2.5 text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[20px]">
              <Link
                href={`/media/${detail.media.kind}/${detail.media.tmdb_id}`}
                className="truncate hover:underline"
              >
                {detail.media.title}
              </Link>
              <span className="tnum shrink-0 text-body font-normal text-white/50">
                {detail.media.year ?? ""}
              </span>
            </h1>

            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sub text-white/70">
              <span className="flex items-center gap-1.5">
                <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.color }} />
                {meta.label} · {subscriptionProgressNote(detail)}
              </span>
              <span className="text-white/45">订阅于 {formatDateTime(detail.created_at)}</span>
            </p>

            {/* 参数徽片：我订了什么 */}
            <div className="mt-3.5 flex flex-wrap gap-2">
              {!isMovie && (
                <ParamChip>
                  {detail.selected_seasons.length > 0
                    ? `勾选第 ${detail.selected_seasons.join("、")} 季`
                    : "未勾选季"}
                </ParamChip>
              )}
              {!isMovie && <ParamChip>持续追新 {detail.follow_future ? "开" : "关"}</ParamChip>}
              {qualityPolicyNote && <ParamChip>{qualityPolicyNote}</ParamChip>}
              {/* 规则组徽片可点击换组：删除被引用规则组前"先把订阅改到其他组"的
                  唯一 Web 入口，也是新建规则组后应用到已有订阅的路 */}
              <button
                type="button"
                onClick={() => setSwitchingRule(true)}
                title="更换本订阅使用的规则组"
                className="rounded-full bg-white/[0.09] px-2.5 py-1 text-caption text-white/75 backdrop-blur-sm transition hover:bg-white/[0.18] hover:text-white"
              >
                规则组「{ruleSetName}」<span className="ml-0.5 text-white/50">更换 ›</span>
              </button>
              {/* 调整订阅：季选择/追新/入库库（后端 diff 重算工单，无需取消重订） */}
              <button
                type="button"
                onClick={() => setAdjusting(true)}
                title={isMovie ? "更换入库目标库" : "修改季选择、持续追新或入库目标库"}
                className="rounded-full bg-white/[0.09] px-2.5 py-1 text-caption text-white/75 backdrop-blur-sm transition hover:bg-white/[0.18] hover:text-white"
              >
                调整订阅<span className="ml-0.5 text-white/50">›</span>
              </button>
            </div>

            <ProgressStrip progress={detail.progress} />
          </div>

          <div className="flex shrink-0 flex-wrap gap-2.5 pt-0.5 max-md:w-full">
            {/* 缺口存在且未暂停时才有意义；其余情况后端会给可读错误，按钮直接隐藏更干净 */}
            {detail.progress.wanted + detail.progress.upgrading > 0 &&
              detail.status !== "paused" && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void searchNow()}
                className="btn-accent h-9 rounded-full px-4 text-sub font-semibold disabled:opacity-40"
              >
                立即搜索
              </button>
            )}
            {detail.progress.wanted + detail.progress.upgrading > 0 && (
              <Link
                href={
                  `/search?q=${encodeURIComponent(detail.media.title)}&for_sub=${detail.id}` as Route
                }
                className="btn-glass h-9 bg-white/10 px-4 text-sub font-medium backdrop-blur-md"
                title="到站点资源搜索里挑一条种子，直接投给本订阅（跳过规则组限制）"
              >
                手动选种
              </Link>
            )}
            <button
              type="button"
              disabled={busy || detail.status === "completed"}
              onClick={togglePause}
              className="btn-glass h-9 bg-white/10 px-4 text-sub font-medium backdrop-blur-md disabled:opacity-40"
            >
              {detail.status === "paused" ? "恢复追踪" : "暂停"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={remove}
              className="h-9 rounded-full border border-red-400/30 bg-red-500/10 px-4 text-sub font-medium text-red-200 transition hover:bg-red-500/20 disabled:opacity-40"
            >
              取消订阅
            </button>
          </div>
        </div>
      </div>

      {/* —— 2. 标签页：状态快照与事件流水分页各占全宽 —— */}
      <div className="text-on-image mt-7 flex items-center gap-1.5">
        <InspectorTab
          active={tab === "wanted"}
          onClick={() => setTab("wanted")}
          label="追踪明细"
          count={detail.progress.total}
        />
        <InspectorTab
          active={tab === "activity"}
          onClick={() => setTab("activity")}
          label="活动记录"
          count={activities.length}
        />
        {/* 解释性副文案：窄屏上它会跟两颗标签胶囊抢宽度，把「追踪明细」挤成两行
            （iPhone 实测），而它本身只是提示、不承载数据——移动端直接不显示。
            桌面端也补 truncate 兜底，窄窗口时截断而不是撑破这一行。 */}
        <span className="ml-2 truncate text-sub text-[var(--text-faint)] max-md:hidden">
          {tab === "wanted" ? "每个追踪单元此刻到哪一步了" : "系统对该订阅的每个动作"}
        </span>
      </div>

      <div className="mt-4">
        {tab === "wanted" ? (
          <WantedBreakdown wanted={detail.wanted} isMovie={isMovie} downloads={downloadByHash} />
        ) : (
          <ActivityTimeline activities={activities} />
        )}
      </div>

      {adjusting && (
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

      {switchingRule && (
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
                    <span className="shrink-0 text-caption font-medium text-[#4ade80]">
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

/** Hero 里的参数徽片：无边框纯填充胶囊（全站「无线框」原则）。 */
function ParamChip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full bg-white/[0.09] px-2.5 py-1 text-caption text-white/75 backdrop-blur-sm">
      {children}
    </span>
  );
}

/** 三段式进度条：绿=已入库（终态）、蓝=下载中（在途）、底轨=缺口。 */
function ProgressStrip({
  progress,
}: {
  progress: SubscriptionDetail["progress"];
}) {
  const { total, wanted, grabbed, downloaded, imported, upgrading } = progress;
  const denom = Math.max(total, 1);
  const inPipeline = grabbed + downloaded;
  return (
    <div className="mt-4">
      <div className="flex h-1.5 w-full max-w-[420px] overflow-hidden rounded-full bg-white/[0.12]">
        <div
          className="bg-[#4ade80]"
          style={{ width: `${(imported / denom) * 100}%` }}
        />
        <div
          className="bg-[#6aa7ff]"
          style={{ width: `${(inPipeline / denom) * 100}%` }}
        />
        <div
          className="bg-[#f5c451]"
          style={{ width: `${(upgrading / denom) * 100}%` }}
        />
      </div>
      <p className="tnum mt-2 text-sub text-white/55">
        共 {total} 项 · 缺 {wanted}
        {inPipeline > 0 && ` · 下载中 ${inPipeline}`}
        {upgrading > 0 && ` · 洗版中 ${upgrading}`}
        {imported > 0 && ` · 已入库 ${imported}`}
      </p>
    </div>
  );
}

/** 标签页切换钮：与影片详情页「剧照/海报」同款胶囊，带计数。 */
function InspectorTab({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      /* shrink-0 + whitespace-nowrap：标签是固定宽度的控件，任何情况下都不该
         被同行的副文案压缩换行（这正是移动端「追踪明细」折成两行的直接原因） */
      className={`flex shrink-0 items-center gap-2 whitespace-nowrap rounded-full px-4 py-1.5 text-ui font-medium transition-colors ${
        active
          ? "bg-white/[0.14] text-white"
          : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
      }`}
    >
      {label}
      <span className="tnum text-caption opacity-70">{count}</span>
    </button>
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
      return "#4ade80";
    case "match_rejected":
    case "dispatch_failed":
    case "import_failed":
      return "#f87171";
    case "paused":
      return "#f5c451";
    default:
      return "#6aa7ff";
  }
}

/**
 * 活动时间线（全宽竖轨）：圆点定性（颜色）+ 中文流水句 + 相对时间。
 * message 由后端写入时渲染成完整句子，前端不做模板拼接。
 */
function ActivityTimeline({ activities }: { activities: SubscriptionActivity[] }) {
  if (activities.length === 0) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-5 text-sub leading-6 text-[var(--text-muted)] backdrop-blur-xl">
        暂无活动记录。系统开始搜索、匹配或投递后，每个动作都会记录在这里。
      </p>
    );
  }
  return (
    <div className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] px-6 py-5 backdrop-blur-xl">
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
                  style={{ backgroundColor: color, boxShadow: `0 0 8px ${color}55` }}
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

/** 追踪项按季分组展开；电影是单项的退化形态。 */
function WantedBreakdown({
  wanted,
  isMovie,
  downloads,
}: {
  wanted: WantedItem[];
  isMovie: boolean;
  /** info_hash → 实时下载快照（无在途工单时为空 Map） */
  downloads: Map<string, SubscriptionDownload>;
}) {
  if (wanted.length === 0) {
    return (
      <p className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-5 text-sub leading-6 text-[var(--text-muted)] backdrop-blur-xl">
        当前没有追踪项。开启「持续追新」后，新集播出会自动加入。
      </p>
    );
  }

  const seasons = new Map<number, WantedItem[]>();
  for (const w of wanted) {
    const list = seasons.get(w.season_number) ?? [];
    list.push(w);
    seasons.set(w.season_number, list);
  }

  return (
    <div className="space-y-4">
      {[...seasons.entries()]
        .sort(([a], [b]) => a - b)
        .map(([season, items]) => (
          <div
            key={season}
            className="overflow-hidden rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] backdrop-blur-xl"
          >
            {!isMovie && (
              <p className="border-b border-white/[0.06] px-5 py-2.5 text-sub font-semibold text-white/80">
                {season === 0 ? "特别篇" : `第 ${season} 季`}
                <span className="ml-2 font-normal text-[var(--text-faint)]">
                  {items.filter((w) => !["wanted", "upgrading"].includes(w.status)).length}/
                  {items.length} 已安排
                </span>
              </p>
            )}
            <ul className="divide-y divide-white/[0.05]">
              {items.map((w) => (
                <WantedRow
                  key={w.id}
                  wanted={w}
                  isMovie={isMovie}
                  download={w.info_hash ? downloads.get(w.info_hash) : undefined}
                />
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}

/**
 * 单个追踪项的透明化行：状态徽标 + 该项此刻"卡在哪、下一步是什么"。
 * 文案与后端调度语义一一对应（补旧排队 / 追新等被动匹配 / 未定档不可调度）。
 */
function WantedRow({
  wanted: w,
  isMovie,
  download,
}: {
  wanted: WantedItem;
  isMovie: boolean;
  /** 该工单锚定种子的实时下载快照（仅在途工单有，5s 轮询更新） */
  download?: SubscriptionDownload;
}) {
  const { label, color, note } = wantedPresentation(w);
  // 已提交下载且拿到了实时快照：说明行升级为进度行（进度条 + 速度/ETA）
  const live = w.status === "grabbed" || w.status === "downloaded" ? download : undefined;
  return (
    /* 移动端：顶部对齐 + 更紧的间距——说明文案在窄屏允许折行（见下方 md:truncate），
       折行后徽标要与文案首行齐平，而不是吊在两行的正中 */
    <li className="px-5 py-2.5 max-md:px-4">
      <div className="flex items-center gap-4 max-md:items-start max-md:gap-3">
        <span className="tnum w-14 shrink-0 text-sub font-medium text-white/90">
          {isMovie ? "正片" : `E${String(w.episode_number).padStart(2, "0")}`}
        </span>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-micro font-semibold"
          style={{ backgroundColor: `${color}22`, color }}
        >
          {label}
        </span>
        {/* 桌面端一行截断（列表要能快速扫读）；移动端改为折行——窄屏截断后只剩
            半个日期（「将于 202…」），信息量归零，不如让它占两行把话说完 */}
        <span className="tnum min-w-0 flex-1 text-sub leading-5 text-[var(--text-muted)] md:truncate">
          {live ? downloadNote(live) : note}
        </span>
        {!live && w.search_attempts > 0 && (
          <span className="tnum shrink-0 text-caption text-[var(--text-faint)]">
            已搜索 {w.search_attempts} 次
          </span>
        )}
      </div>
      {live && live.progress != null && live.state !== "missing" && (
        <div
          className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.08]"
          role="progressbar"
          aria-valuenow={Math.round(live.progress * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full rounded-full bg-[#34d399] transition-[width] duration-700"
            style={{ width: `${Math.min(100, Math.max(1, live.progress * 100))}%` }}
          />
        </div>
      )}
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

function wantedPresentation(w: WantedItem): { label: string; color: string; note: string } {
  if (w.status === "upgrading") {
    return {
      label: "洗版中",
      color: "#f5c451",
      note: w.last_search_at
        ? `已有可用版本，上次搜索 ${formatRelativeTime(w.last_search_at)}`
        : "已有可用版本，正在寻找洗版目标",
    };
  }
  if (w.status === "imported") {
    return { label: "已入库", color: "#4ade80", note: `入库于 ${formatDateTime(w.imported_at)}` };
  }
  if (w.status === "downloaded") {
    return {
      label: "已下载",
      color: "#4ade80",
      note: `完成于 ${formatDateTime(w.downloaded_at ?? w.grabbed_at)}，待整理入库`,
    };
  }
  if (w.status === "grabbed") {
    return {
      label: "已提交下载",
      color: "#34d399",
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
      note: "播出日期未公布，定档后自动排队",
    };
  }
  const due = new Date(w.next_search_at);
  if (w.air_date && new Date(w.air_date) > new Date()) {
    return {
      label: "待播出",
      color: "#f5c451",
      note: `${w.air_date} 播出，${formatDateTime(w.next_search_at)} 起兜底搜索`,
    };
  }
  if (due <= new Date()) {
    return {
      label: "排队搜索",
      color: "#6aa7ff",
      note: w.last_search_at
        ? `上次搜索 ${formatRelativeTime(w.last_search_at)}`
        : "等待搜索任务执行",
    };
  }
  return {
    label: "冷却中",
    color: "#6aa7ff",
    note: `暂无合适资源，${formatDateTime(w.next_search_at)} 再试`,
  };
}
