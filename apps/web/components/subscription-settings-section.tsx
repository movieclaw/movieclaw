"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Link from "next/link";

import type { MediaSearchItem } from "@/lib/api/discover";
import { searchTitles } from "@/lib/api/search";
import { RuleSetsPanel } from "@/components/rule-sets-panel";
import {
  checkSubscriptionAutomationReadiness,
  previewSubscriptionDownloadRouting,
  type DispatchPreview,
  type FixOption,
  type LibraryPipeline,
  type PipelineCheck,
  type PipelineHealth,
  type PipelineIssue,
} from "@/lib/api/subscriptions";

/**
 * 订阅规则（设置 → 订阅规则）：订阅相关配置的家。
 *
 * 两个住户：
 * - 模拟一单：搜一部片，把路由选库/投递落点/入库方式完整预演一遍，
 *   不真正订阅——让用户主动验证自己对系统的理解；
 * - 规则组：完整管理入口（新建/编辑/删除，见 rule-sets-panel.tsx）。
 *
 * 链路体检（PipelineHealthPanel）原本也住这里，设置页按功能重组后升级为
 * 「概览」落地页的主体（settings-overview-section.tsx）——它体检的是整条
 * 链路而非订阅规则本身，放在设置入口处让问题主动找人。面板仍定义在本文件：
 * 它与模拟一单共享状态语义（STATUS_META）与修复跳转（fixTarget）。
 */
export function SubscriptionSettingsSection() {
  return (
    <div className="space-y-10">
      <SimulatePanel />
      <RuleSetsPanel />
    </div>
  );
}

/** 状态 → 颜色/文案（节点、检查项、库行共用一套语义）。 */
const STATUS_META = {
  ok: { dot: "bg-[var(--ok)]", text: "text-[var(--ok)]", label: "正常" },
  warn: { dot: "bg-[var(--warn)]", text: "text-[var(--warn)]", label: "降级" },
  error: { dot: "bg-[var(--danger)]", text: "text-[var(--danger)]", label: "有问题" },
} as const;

type Status = keyof typeof STATUS_META;

/** 修复去处 → 跳转地址与文案。 */
function fixTarget(section: string | null): { href: string; label: string } | null {
  if (section === "sites") return { href: "/settings/sites", label: "去接入站点" };
  if (section === "downloaders") return { href: "/settings/downloaders", label: "去下载器设置" };
  if (section === "import-watch") return { href: "/settings/import-watch", label: "去自动入库" };
  if (section === "libraries") return { href: "/library", label: "去媒体库" };
  return null;
}

/** 修复选项 → 带预填参数的跳转地址（目标设置页读取参数自动填表单）。 */
function fixOptionHref(option: FixOption): string | null {
  const target = fixTarget(option.fix_section);
  if (!target) return null;
  if (!option.fix_params) return target.href;
  return `${target.href}?${new URLSearchParams(option.fix_params)}`;
}

function worst(statuses: Status[]): Status {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("warn")) return "warn";
  return "ok";
}

// ---------------------------------------------------------------------------
// 链路体检
// ---------------------------------------------------------------------------

export function PipelineHealthPanel() {
  const [health, setHealth] = useState<PipelineHealth | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    setBusy(true);
    checkSubscriptionAutomationReadiness()
      .then(setHealth)
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 修完配置回到本页自动重检：修复 → 回来 → 变绿的反馈闭环
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [reload]);

  // 开局清单条件：三个必要件（站点/下载器/媒体库）任一**从未配置**。
  // 用"配置过"而非"当前可用"判断——站点 cookie 过期的老用户该看到的是
  // 体检红项，不是新手三步清单
  const setupNeeded =
    health !== null &&
    (!health.sites_configured || !health.downloaders_configured || health.libraries.length === 0);

  // 已聚合成修复卡的检查项 key：库卡片默认不再重复其红项文字（状态点保留）
  const issueKeys = new Set((health?.issues ?? []).map((i) => i.key));

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-body font-semibold text-white/90">订阅链路体检</h3>
        <button
          type="button"
          onClick={reload}
          disabled={busy}
          className="btn-glass flex items-center gap-1.5 px-3 py-1.5 text-sub font-medium disabled:opacity-60"
        >
          {busy && (
            <span className="size-3 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          )}
          重新体检
        </button>
      </div>
      <p className="mb-4 text-sub leading-6 text-[var(--text-muted)]">
        逐库预演「资源搜索 → 下载 → 投递 → 入库」的完整链路，与真实投递同一套判定。
        红点表示订阅会卡在那一步（工单不会丢，修好后自动重试）；黄点能转但有降级。
        修改配置后回到本页会自动重检。
      </p>

      {failed && (
        <p className="rounded-xl bg-white/[0.03] px-4 py-5 text-center text-ui text-[var(--text-muted)]">
          体检加载失败，请重试
        </p>
      )}
      {health === null && !failed && (
        <p className="flex items-center gap-2.5 rounded-xl bg-white/[0.03] px-4 py-5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在体检…
        </p>
      )}

      {health !== null && setupNeeded && <SetupChecklist health={health} />}

      {health !== null && !setupNeeded && (
        <div className="space-y-3">
          {/* 修复卡置顶：按根因聚合，「需要处理 N 件事」——一个根因一张卡，
              不随受影响的库数膨胀；库卡片里对应的红项文字随之隐藏（状态点保留） */}
          {health.issues.length > 0 && <IssueCardList issues={health.issues} />}
          {/* 公共段：资源搜索 + 下载器（所有库共享，不逐库重复） */}
          <SharedSegmentsRow health={health} hiddenKeys={issueKeys} />
          {health.libraries.map((pipeline) => (
            <LibraryPipelineCard
              key={pipeline.library_id}
              pipeline={pipeline}
              hiddenKeys={issueKeys}
            />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * 开局清单：三个必要件缺任一时，体检数据换一种读法——"下一步做什么"。
 * 全部配齐后本清单自动让位给正常体检，同一页面从第一天用到第一百天。
 */
function SetupChecklist({ health }: { health: PipelineHealth }) {
  const steps: { done: boolean; label: string; hint: string; href: string; action: string }[] = [
    {
      done: health.sites_configured,
      label: "接入资源站点",
      hint: "订阅从这里搜索资源",
      href: "/settings/sites",
      action: "去接入",
    },
    {
      done: health.downloaders_configured,
      label: "接入下载器",
      hint: "qBittorrent / Transmission，找到的资源交给它下载",
      href: "/settings/downloaders",
      action: "去接入",
    },
    {
      done: health.libraries.length > 0,
      label: "创建媒体库",
      hint: "内容的家：下载完成后按「标题 (年份)」整理进库",
      href: "/library",
      action: "去创建",
    },
  ];
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] p-4">
      <p className="mb-3 text-ui font-medium text-white/90">
        把订阅跑起来需要三步（完成后这里会变成链路体检）：
      </p>
      <div className="space-y-2">
        {steps.map((step, i) => (
          <div key={step.label} className="flex items-center gap-3">
            <span
              className={`flex size-5 shrink-0 items-center justify-center rounded-full text-caption font-semibold ${
                step.done ? "bg-[var(--ok)]/20 text-[var(--ok)]" : "bg-white/10 text-white/70"
              }`}
            >
              {step.done ? "✓" : i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className={`text-ui font-medium ${step.done ? "text-white/50 line-through" : "text-white/90"}`}
              >
                {step.label}
              </span>
              <span className="ml-2 text-caption text-[var(--text-faint)]">{step.hint}</span>
            </span>
            {!step.done && (
              <Link
                href={step.href as never}
                className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
              >
                {step.action}
              </Link>
            )}
          </div>
        ))}
      </div>
      <p className="mt-3 text-caption leading-relaxed text-[var(--text-faint)]">
        可选第四步：「自动入库」让下载区与媒体库分离（PT 保种推荐）——下载器落盘的
        内容自动硬链接进库，源文件继续做种。不配置则直接下载进库根，同样能自动入账。
      </p>
    </div>
  );
}

/**
 * 修复卡列表：「需要处理 N 件事」。
 * 一张卡 = 一个根因，卡内列出受影响的库与结构化的修复选项（多选项时
 * 由用户按自己的部署取舍，每个选项讲清做什么/为什么/跳到哪）。
 */
function IssueCardList({ issues }: { issues: PipelineIssue[] }) {
  return (
    <div className="space-y-2.5">
      <p className="text-ui font-medium text-white/90">
        需要处理 {issues.length} 件事（下方库卡片的红黄点都来自这里）：
      </p>
      {issues.map((issue) => (
        <IssueCard key={issue.key + issue.title} issue={issue} />
      ))}
    </div>
  );
}

function IssueCard({ issue }: { issue: PipelineIssue }) {
  const isError = issue.status === "error";
  return (
    <div
      className={`rounded-xl border p-4 ${
        isError ? "border-[#ff6b6b]/30 bg-[#ff6b6b]/[0.06]" : "border-amber-400/25 bg-amber-500/[0.06]"
      }`}
    >
      <div className="flex items-start gap-2.5">
        <span className={`mt-[5px] size-2 shrink-0 rounded-full ${STATUS_META[issue.status].dot}`} />
        <div className="min-w-0 flex-1">
          <p className="text-ui font-semibold text-white/95">{issue.title}</p>
          {issue.affected_libraries.length > 0 && (
            <p className="mt-1 text-caption text-[var(--text-faint)]">
              影响 {issue.affected_libraries.length} 个库：{issue.affected_libraries.join("、")}
            </p>
          )}
          <p className="mt-1.5 text-sub leading-relaxed text-[var(--text-muted)]">
            {issue.detail}
          </p>
        </div>
      </div>
      {issue.options.length > 0 && (
        <div
          className={`mt-3 grid gap-2.5 ${issue.options.length > 1 ? "md:grid-cols-2" : ""}`}
        >
          {issue.options.map((option, i) => (
            <FixOptionBlock
              key={option.title}
              option={option}
              ordinal={issue.options.length > 1 ? i + 1 : null}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** 修复卡里的一个选项块：做什么 / 为什么 / 带预填参数的跳转。 */
function FixOptionBlock({ option, ordinal }: { option: FixOption; ordinal: number | null }) {
  const href = fixOptionHref(option);
  return (
    <div className="flex flex-col rounded-lg bg-white/[0.04] p-3">
      <p className="text-sub font-semibold text-white/90">
        {ordinal !== null && (
          <span className="mr-1.5 inline-flex size-4.5 items-center justify-center rounded-full bg-white/10 text-micro text-white/70">
            {ordinal}
          </span>
        )}
        {option.title}
      </p>
      {option.why && (
        <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">{option.why}</p>
      )}
      <p className="mt-1.5 flex-1 text-sub leading-relaxed text-[var(--text-muted)]">
        {option.steps}
      </p>
      {href && (
        <Link
          href={href as never}
          className="btn-glass mt-2.5 self-start px-3 py-1.5 text-sub font-medium"
        >
          {option.fix_label} →
        </Link>
      )}
    </div>
  );
}

/** 流水线上的一个节点（步骤条渲染单元）。 */
interface FlowNode {
  key: string;
  label: string;
  sub: string | null; // 节点下的一行小字（下载器名/路径等）
  status: Status;
  checks: PipelineCheck[]; // 该节点聚合的检查项（红黄项列在卡片下方）
}

/** 步骤条：节点胶囊 + 连接线，红黄节点自然显眼。 */
function FlowStepper({ nodes }: { nodes: FlowNode[] }) {
  return (
    <div className="flex flex-wrap items-center gap-y-2">
      {nodes.map((node, i) => (
        <div key={node.key} className="flex items-center">
          {i > 0 && <span className="mx-1.5 h-px w-4 shrink-0 bg-white/20" />}
          <div
            className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 ${
              node.status === "ok"
                ? "border-white/[0.08] bg-white/[0.03]"
                : node.status === "warn"
                  ? "border-amber-400/30 bg-amber-500/10"
                  : "border-[#ff6b6b]/35 bg-[#ff6b6b]/10"
            }`}
          >
            <span className={`size-1.5 shrink-0 rounded-full ${STATUS_META[node.status].dot}`} />
            <span className="text-sub font-medium text-white/90">{node.label}</span>
            {node.sub && (
              <span
                dir="rtl"
                className="max-w-[180px] truncate font-mono text-caption text-[var(--text-faint)]"
                title={node.sub}
              >
                {"‎" + node.sub + "‎"}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 公共段（资源搜索 + 下载器）：所有库共享，单独一行不逐库重复。 */
function SharedSegmentsRow({
  health,
  hiddenKeys,
}: {
  health: PipelineHealth;
  /** 已被修复卡聚合的检查项 key：红项文字不再重复（状态点保留） */
  hiddenKeys: Set<string>;
}) {
  const downloaderCheck = health.libraries[0]?.checks.find((c) => c.key === "downloader");
  const nodes: FlowNode[] = [
    {
      key: "sites",
      label: "资源搜索",
      sub: null,
      status: health.site_check.status,
      checks: [health.site_check],
    },
    {
      key: "downloader",
      label: "下载器",
      sub: null,
      status: downloaderCheck?.status ?? (health.downloader_ok ? "ok" : "error"),
      checks: downloaderCheck ? [downloaderCheck] : [],
    },
  ];
  const problems = nodes
    .flatMap((n) => n.checks)
    .filter((c) => c.status !== "ok" && !hiddenKeys.has(c.key));
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="w-24 shrink-0 text-sub text-[var(--text-faint)] max-md:hidden">
          公共链路
        </span>
        <FlowStepper nodes={nodes} />
      </div>
      {problems.length > 0 && <ProblemList checks={problems} />}
    </div>
  );
}

/** 一个库的流水线卡片：步骤条 + 叙事 + 红黄项详情（已聚合的红项让位给修复卡）。 */
function LibraryPipelineCard({
  pipeline,
  hiddenKeys,
}: {
  pipeline: LibraryPipeline;
  /** 已被修复卡聚合的检查项 key：红项文字不再逐库重复（状态点保留） */
  hiddenKeys: Set<string>;
}) {
  const [showAll, setShowAll] = useState(false);

  const byKey = (keys: string[]) => pipeline.checks.filter((c) => keys.includes(c.key));
  const dispatchChecks = byKey(["dispatch_dir", "mapping"]);
  const transferChecks = byKey(["transfer_disk", "watch_active"]);
  const ingestError = pipeline.checks.some(
    (c) => c.key === "dispatch_dir" && c.fix_section === "libraries" && c.status !== "ok",
  );

  const nodes: FlowNode[] = [
    {
      key: "dispatch",
      label: pipeline.mode === "watch" ? "投递到监听目录" : "投递",
      sub: pipeline.path,
      status: worst(dispatchChecks.map((c) => c.status)),
      checks: dispatchChecks,
    },
  ];
  if (pipeline.mode === "watch") {
    nodes.push({
      key: "transfer",
      label: pipeline.staging_path ? "整理输出" : "转移进库",
      sub: pipeline.staging_path,
      status: worst(transferChecks.map((c) => c.status)),
      checks: transferChecks,
    });
  }
  if (pipeline.staging_path) {
    // 自定义目录规则：整理后不直接入库——中间隔着用户自己的外部流转
    // （上传/转存/人工确认），movieclaw 不判它的死活，节点仅作说明
    nodes.push({
      key: "external",
      label: "外部流转",
      sub: null,
      status: "ok",
      checks: [],
    });
  }
  nodes.push({
    key: "ingest",
    label: pipeline.staging_path ? "回流入账" : "入库",
    sub: pipeline.library_root,
    status: ingestError ? "error" : "ok",
    checks: [],
  });

  // 卡片下方默认只列**未被修复卡聚合**的红黄项；「查看全部」仍展开全部事实
  const problems = pipeline.checks.filter((c) => c.status !== "ok" && !hiddenKeys.has(c.key));
  const listed = showAll ? pipeline.checks.filter((c) => c.key !== "downloader") : problems;
  const meta = STATUS_META[pipeline.status];

  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="w-24 shrink-0 max-md:hidden">
          <span className="block truncate text-ui font-medium text-white/90">
            {pipeline.library_name}
          </span>
          <span className="block text-caption text-[var(--text-faint)]">
            {pipeline.kind === "movie" ? "电影" : "剧集"}
            {pipeline.is_default ? " · 默认" : ""}
          </span>
        </span>
        <div className="min-w-0 flex-1">
          <p className="mb-1.5 text-sub font-medium text-white/90 md:hidden">
            {pipeline.library_name}
          </p>
          <FlowStepper nodes={nodes} />
          {/* 正向叙事：订阅命中本库会发生什么——全绿时的可预期，不用去玩模拟一单 */}
          {pipeline.narrative && (
            <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
              {pipeline.narrative}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span className={`text-sub font-medium ${meta.text}`}>{meta.label}</span>
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="text-caption text-[var(--text-faint)] hover:text-white"
          >
            {showAll ? "收起" : "查看全部"}
          </button>
        </div>
      </div>
      {listed.length > 0 && <ProblemList checks={listed} />}
    </div>
  );
}

/** 检查项明细列表（红黄项默认展示；「查看全部」时含绿项）。 */
function ProblemList({ checks }: { checks: PipelineCheck[] }) {
  return (
    <div className="mt-2.5 space-y-1.5 border-t border-white/[0.06] pt-2.5">
      {checks.map((check) => {
        const fix = check.status !== "ok" ? fixTarget(check.fix_section) : null;
        return (
          <div key={check.key + check.detail} className="flex items-start gap-2.5">
            <span
              className={`mt-[5px] size-1.5 shrink-0 rounded-full ${STATUS_META[check.status].dot}`}
            />
            <p className="min-w-0 flex-1 text-sub leading-relaxed text-[var(--text-muted)]">
              <span className="font-medium text-[var(--text)]">{check.label}</span>
              <span className="ml-2">{check.detail}</span>
              {fix && (
                <Link
                  href={fix.href as never}
                  className="ml-2 whitespace-nowrap font-medium text-[var(--accent)] hover:underline"
                >
                  {fix.label} →
                </Link>
              )}
            </p>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 模拟一单：搜一部片，把完整决策过程演一遍（不真正订阅）
// ---------------------------------------------------------------------------

function SimulatePanel() {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<MediaSearchItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<MediaSearchItem | null>(null);
  const [preview, setPreview] = useState<DispatchPreview | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 输入去抖搜索：只取带类型的 TMDB 条目（路由需要 movie/tv 先验）
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    setPicked(null);
    setPreview(null);
    if (query.trim().length < 2) {
      setCandidates(null);
      return;
    }
    timer.current = setTimeout(() => {
      setSearching(true);
      searchTitles(query.trim(), { provider: "tmdb" })
        .then(({ items }) => setCandidates(items.filter((it) => it.type).slice(0, 6)))
        .catch(() => setCandidates([]))
        .finally(() => setSearching(false));
    }, 400);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query]);

  const pick = (item: MediaSearchItem) => {
    setPicked(item);
    setPreview(null);
    if (!item.type) return;
    void previewSubscriptionDownloadRouting(item.type, null, Number(item.id), {
      title: item.title,
      year: item.year,
    })
      .then(setPreview)
      .catch(() => setPreview(null));
  };

  return (
    <section>
      <h3 className="mb-2 text-body font-semibold text-white/90">模拟一单</h3>
      <p className="mb-3 text-sub leading-6 text-[var(--text-muted)]">
        搜一部片，看它订阅后会进哪个库、投递到哪、怎么入库——只做预演，不会真的订阅。
        配完收藏范围想确认「某类片会进哪」，在这里试一下就知道。
      </p>
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="输入片名，如：葬送的芙莉莲"
        autoComplete="off"
        className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
      />
      {searching && (
        <p className="mt-2 text-sub text-[var(--text-faint)]">正在搜索…</p>
      )}
      {candidates !== null && !searching && !picked && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {candidates.length === 0 && (
            <p className="text-sub text-[var(--text-faint)]">没有找到条目，换个关键词试试</p>
          )}
          {candidates.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => pick(item)}
              className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
            >
              {item.title}
              <span className="ml-1.5 text-[var(--text-faint)]">
                {item.year ?? ""} {item.type === "movie" ? "电影" : "剧集"}
              </span>
            </button>
          ))}
        </div>
      )}
      {picked && (
        <div className="mt-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
          <p className="text-ui font-medium text-white/90">
            《{picked.title}》{picked.year ? ` (${picked.year})` : ""}
          </p>
          {preview === null ? (
            <p className="mt-2 text-sub text-[var(--text-faint)]">正在预演…</p>
          ) : (
            <div className="mt-2 space-y-1.5">
              <SimStep
                n={1}
                text={
                  preview.route_reason ??
                  (preview.library_name ? `入库到「${preview.library_name}」` : "没有可用的媒体库")
                }
              />
              <SimStep
                n={2}
                text={
                  preview.mode === "watch"
                    ? preview.staging_path
                      ? `投递到自动入库的监听目录 ${preview.path}，下载完成后识别改名并整理到 ${preview.staging_path}`
                      : `投递到自动入库的监听目录 ${preview.path}，下载完成后自动整理入库`
                    : preview.mode === "inplace"
                      ? // 条目目录由后端按命名模板渲染，前端不自己拼「标题 (年份)」
                        `直接下载到库内目录 ${(preview.entry_dir ?? preview.path)?.replace(/\/+$/, "")}，完成后自动入账`
                      : "落到下载器默认目录（不会自动入库）"
                }
              />
              {preview.staging_path && (
                <SimStep
                  n={3}
                  text="等待文件进入媒体库根目录后自动扫描入账（上传/转存等外部流转由你的工具完成）"
                />
              )}
              {preview.ok ? (
                <p className="flex items-center gap-1.5 pt-1 text-sub font-medium text-[var(--ok)]">
                  ✓ 全链路可行：订阅后即可自动下载并整理入库
                </p>
              ) : (
                <p className="rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-sub leading-relaxed text-amber-200">
                  {preview.warning}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function SimStep({ n, text }: { n: number; text: string }) {
  return (
    <p className="flex items-start gap-2.5 text-sub leading-relaxed text-[var(--text-muted)]">
      <span className="mt-px flex size-4.5 shrink-0 items-center justify-center rounded-full bg-white/10 text-micro font-semibold text-white/70">
        {n}
      </span>
      <span className="min-w-0 flex-1">{text}</span>
    </p>
  );
}
