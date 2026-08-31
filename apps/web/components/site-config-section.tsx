"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { useConfirm } from "@/components/feedback";
import { Modal } from "@/components/modal";
import {
  ChevronDownIcon,
  MoreIcon,
  PlusIcon,
  ServerIcon,
  ShieldIcon,
} from "@/components/icons";
import { ExtensionCard } from "@/components/extension-settings";
import { SearchSection } from "@/components/search-settings";
import { useTabParam } from "@/lib/use-tab-param";
import type { ConfiguredSite, SiteAuthType, SiteStatus } from "@/lib/api/extension";
import {
  type AuthTypeRequirement,
  type CatalogItem,
  type SiteBoostStats,
  type SiteConfigPayload,
  type SiteSyncStats,
  configureSite,
  deleteSite,
  listConfiguredSites,
  listSiteBoostStats,
  listSiteCatalog,
  listSiteSyncStats,
  reverifySite,
  setSiteBoostPaused,
  setSiteEnabled,
  setSiteProtection,
  setSiteRatioBoost,
  updateSite,
} from "@/lib/api/sites";
import Link from "next/link";
import type { Route } from "next";

import { useDownloadTasks } from "@/lib/download-tasks";
import { formatBytes, formatCompact, formatDuration, formatRatio } from "@/lib/format";
import { cachedImageUrl } from "@/lib/image-proxy";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/*
 * 页面信息架构（docs/design/site-protection-ratio-boost.md 之外的 UI 决策）：
 * 每站默认一行，按优先级从左到右排——
 *   P0 常驻：名称 + 验证状态（异常原因直接吃掉徽章位）；
 *   P1 条件徽章：保护中 / 刷流用量与近 24h 产出——开了才出现，不开不占版面；
 *   P2 展开可见：账号统计 / 索引同步 / 刷流设置 / 授权信息；
 *   操作全收进 ⋯ 菜单（启停 / 保护 / 重验 / 删除），卡面只留信息。
 * 展开详情的排版：段标签在桌面抽成左侧固定列（各段内容左缘对齐、统计纵向成列），
 *   统计走等宽列（2 → 3 → 4 列）+ 等宽数字，段与段之间用发丝线分隔——
 *   替代原来的「标签在上 + 自然排布」，后者在宽屏上列宽参差、留白散。
 *   统计刻意不加底色小卡：层级靠对齐与字重撑住，加卡片会把这页的「轻」丢掉。
 * 视觉刻意不用玻璃质感与 WebGL 开关：配置页要的是轻和稳（移动端尤其），
 * 玻璃留给首页与海报墙等展示面。移动端徽章行自动折到第二行，整行是
 * 展开热区。异常站点置顶 + 顶部健康摘要，把「是否正常」从逐卡看变成一行知。
 */

/** 站点验证状态 → 展示文案与颜色 */
const STATUS_META: Record<SiteStatus, { label: string; color: string }> = {
  active: { label: "已验证", color: "var(--ok)" },
  verifying: { label: "验证中", color: "var(--info)" },
  pending: { label: "待验证", color: "#c0c4cc" },
  failed: { label: "验证失败", color: "var(--danger)" },
};

/** 授权类型 → 中文名 */
const AUTH_TYPE_LABEL: Record<SiteAuthType, string> = {
  cookie: "Cookie",
  apikey: "API 密钥",
  credential: "用户名密码",
};

/** 表单字段名 → 中文标签 & 输入类型 */
const FIELD_META: Record<string, { label: string; kind: "text" | "password" | "textarea" }> = {
  cookie: { label: "Cookie 字符串", kind: "textarea" },
  api_key: { label: "API 密钥", kind: "password" },
  username: { label: "用户名", kind: "text" },
  password: { label: "密码", kind: "password" },
};

/** 需要轮询验证进度的中间态 */
const IN_PROGRESS: SiteStatus[] = ["pending", "verifying"];

/** 本分区的两档内容（见 SiteConfigSection 顶部的胶囊标签） */
const TABS = [
  { id: "sites", label: "站点接入" },
  { id: "search", label: "搜索分类" },
] as const;

type SiteTab = (typeof TABS)[number]["id"];

const GIB = 1024 ** 3;

/** 目录中缺失该站点时的兜底展示（例如站点已从系统下架但仍有历史配置） */
function fallbackItem(siteId: string): CatalogItem {
  return { site_id: siteId, display_name: siteId, base_url: "", supported_auth_types: [] };
}

/**
 * 「资源站点」分区的动态副标题（设置头部用）：有站点时显示健康统计——
 * 「已接入 X 个站点，全部正常 / Y 个异常需要关注」；还没接入任何站点时
 * 回落功能介绍文案（fallback），此时统计没有意义、介绍才有引导价值。
 */
export function SitesSectionSubtitle({ fallback }: { fallback: string }) {
  const [sites, setSites] = useState<ConfiguredSite[] | null>(null);
  useEffect(() => {
    let alive = true;
    listConfiguredSites()
      .then((rows) => {
        if (alive) setSites(rows);
      })
      .catch(() => {
        /* 拉取失败保持介绍文案，分区主体会展示具体错误 */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!sites || sites.length === 0) return <>{fallback}</>;
  const failed = sites.filter((s) => s.status === "failed").length;
  if (failed > 0) {
    return (
      <>
        已接入 {sites.length} 个站点，
        <span className="font-medium text-[var(--danger)]">{failed} 个异常需要关注</span>
      </>
    );
  }
  return <>已接入 {sites.length} 个站点，全部正常</>;
}

export function SiteConfigSection() {
  // ?tab=search 深链直达搜索分类，切换写回地址栏（useTabParam，全设置页同一套）
  const [tab, setTab] = useTabParam<SiteTab>(
    TABS.map((t) => t.id),
    "sites",
  );
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [configured, setConfigured] = useState<ConfiguredSite[]>([]);
  // 各站点的种子缓存统计（定时同步任务维护），key 为 site_id；从未同步过的站点没有条目
  const [syncStats, setSyncStats] = useState<Record<string, SiteSyncStats>>({});
  // 各站点的刷流运行统计；从未刷流且未开启刷流的站点没有条目
  const [boostStats, setBoostStats] = useState<Record<string, SiteBoostStats>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 是否展开「添加站点」面板
  const [adding, setAdding] = useState(false);
  // 当前展开详情的站点（单开手风琴）
  const [expandedSite, setExpandedSite] = useState<string | null>(null);
  // 「新建自定义分类」的触发信号：工具栏按钮每点一次 +1，SearchSection 响应打开编辑器
  const [createPresetNonce, setCreatePresetNonce] = useState(0);

  const catalogMap = useMemo(() => new Map(catalog.map((c) => [c.site_id, c])), [catalog]);

  // 已配置集合，供"添加"面板过滤掉已接入的站点
  const configuredIds = useMemo(
    () => new Set(configured.map((s) => s.site_id)),
    [configured],
  );
  const availableItems = useMemo(
    () => catalog.filter((c) => !configuredIds.has(c.site_id)),
    [catalog, configuredIds],
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [cat, cfg, stats, boost] = await Promise.all([
        listSiteCatalog(),
        listConfiguredSites(),
        listSiteSyncStats(),
        listSiteBoostStats(),
      ]);
      setCatalog(cat);
      setConfigured(cfg);
      setSyncStats(stats);
      setBoostStats(boost);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 有站点处于 pending/verifying 时轮询刷新，直到全部落定（页面隐藏时暂停）
  const hasInProgress = configured.some((s) => IN_PROGRESS.includes(s.status));
  useVisiblePolling(
    () => {
      void listConfiguredSites()
        .then(setConfigured)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
    },
    hasInProgress ? 2500 : null,
  );

  // 有站点开着刷流时轻轮询关键数据（引擎 5 分钟一个 tick、同步最快 5 分钟一轮，
  // 30 秒刷新足够跟上）：刷流统计（行级产出读数、详情用量）与索引同步节奏
  // （详情里的上次/下次同步）在页面停留期间自己长，不需要用户手动刷新。
  // 两个请求都是本地聚合查询，不触达任何站点。
  const anyBoosting = configured.some((s) => s.boost_enabled);
  useVisiblePolling(
    () => {
      void listSiteBoostStats()
        .then(setBoostStats)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
      void listSiteSyncStats()
        .then(setSyncStats)
        .catch(() => {
          /* 同上 */
        });
    },
    anyBoosting ? 30000 : null,
  );

  // 原地替换已有站点、新站点追加到末尾。
  // 注意：菜单里的启停/保护等操作也走这里，保持列表基础顺序稳定，避免被操作的站点跳位。
  const upsertConfigured = useCallback((next: ConfiguredSite) => {
    setConfigured((prev) => {
      const idx = prev.findIndex((s) => s.site_id === next.site_id);
      if (idx === -1) return [...prev, next];
      const copy = [...prev];
      copy[idx] = next;
      return copy;
    });
  }, []);

  // 异常站点置顶（稳定排序：failed 之间与正常站之间保持后端顺序）
  const ordered = useMemo(
    () =>
      [...configured].sort(
        (a, b) => (a.status === "failed" ? 0 : 1) - (b.status === "failed" ? 0 : 1),
      ),
    [configured],
  );

  return (
    <div className="space-y-5">
      {/* 工具栏行：左侧胶囊标签切换视图，右侧主操作——描述（分区副标题）之下
          的第一行。接入统计已上移到分区副标题，这里不再重复；刷新按钮省去
          （验证轮询 + 操作后回写已覆盖刷新诉求）。 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex gap-1.5">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-pressed={t.id === tab}
              onClick={() => setTab(t.id)}
              className={`rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
                t.id === tab
                  ? "bg-white/[0.14] text-white"
                  : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {tab === "sites" ? (
          <button
            type="button"
            onClick={() => setAdding((v) => !v)}
            disabled={loading}
            className="btn-accent flex shrink-0 items-center gap-1 rounded-full py-1.5 pl-2.5 pr-3.5 text-sub font-semibold disabled:opacity-60"
          >
            <PlusIcon className="size-4" />
            添加站点
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setCreatePresetNonce((n) => n + 1)}
            className="btn-accent flex shrink-0 items-center gap-1 rounded-full py-1.5 pl-2.5 pr-3.5 text-sub font-semibold"
          >
            <PlusIcon className="size-4" />
            新建自定义分类
          </button>
        )}
      </div>

      {tab === "search" ? (
        <SearchSection createRequest={createPresetNonce} />
      ) : (
        <>
          {error && (
            <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
              {error}
            </div>
          )}

          {/* 下载器拥堵提示：有任务在排队说明活动位满了——新种提交受限、
              刷流已自动暂停投放，引导用户去调大队列上限（一键直达弹窗） */}
          <QueueCongestionTip />

          {/* 「添加站点」面板：从目录里挑选未配置的站点 */}
          {adding && (
            <AddSitePanel
              available={availableItems}
              onCreated={(site) => {
                upsertConfigured(site);
                setAdding(false);
              }}
              onCancel={() => setAdding(false)}
              onError={setError}
            />
          )}

          {/* 站点列表：扁平面板容器，行式布局 */}
          {loading ? (
            <div className="space-y-px overflow-hidden rounded-xl border border-white/[0.08]">
              <div className="h-14 animate-pulse bg-white/[0.04]" />
              <div className="h-14 animate-pulse bg-white/[0.04]" />
            </div>
          ) : configured.length === 0 ? (
            <div className="flex flex-col items-center gap-3 rounded-xl border border-white/[0.08] bg-white/[0.03] px-6 py-12 text-center">
              <span className="icon-chip size-12 !rounded-2xl">
                <ServerIcon className="size-6" />
              </span>
              <div>
                <p className="text-body font-medium text-[var(--text)]">还没有配置任何站点</p>
                <p className="mt-1 text-sub text-[var(--text-muted)]">
                  点击右上角「添加站点」开始接入。
                </p>
              </div>
            </div>
          ) : (
            <div className="divide-y divide-white/[0.06] overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.03]">
              {ordered.map((site) => (
                <SiteRow
                  key={site.site_id}
                  item={catalogMap.get(site.site_id) ?? fallbackItem(site.site_id)}
                  site={site}
                  stats={syncStats[site.site_id]}
                  boost={boostStats[site.site_id]}
                  expanded={expandedSite === site.site_id}
                  onToggle={() =>
                    setExpandedSite((cur) => (cur === site.site_id ? null : site.site_id))
                  }
                  onOpen={() => setExpandedSite(site.site_id)}
                  onChanged={upsertConfigured}
                  onDeleted={(siteId) => {
                    setConfigured((prev) => prev.filter((s) => s.site_id !== siteId));
                    setExpandedSite((cur) => (cur === siteId ? null : cur));
                  }}
                  onError={setError}
                />
              ))}
            </div>
          )}

          {/* 浏览器插件：站点 Cookie 同步的配套工具 */}
          <ExtensionCard />
        </>
      )}
    </div>
  );
}

/* —— 添加站点：先选站点（带搜索），再填授权表单 —— */

interface AddSitePanelProps {
  available: CatalogItem[];
  onCreated: (site: ConfiguredSite) => void;
  onCancel: () => void;
  onError: (message: string) => void;
}

function AddSitePanel({ available, onCreated, onCancel, onError }: AddSitePanelProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogItem | null>(null);
  const [busy, setBusy] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return available;
    return available.filter(
      (c) =>
        c.display_name.toLowerCase().includes(q) ||
        c.site_id.toLowerCase().includes(q) ||
        c.base_url.toLowerCase().includes(q),
    );
  }, [available, query]);

  // 已选定站点 → 展示授权表单
  if (selected) {
    return (
      <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-body font-semibold text-[var(--text)]">
              {selected.display_name}
            </p>
            <p className="truncate text-caption text-[var(--text-faint)]">{selected.base_url}</p>
          </div>
          <button
            type="button"
            onClick={() => setSelected(null)}
            disabled={busy}
            className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
          >
            重新选择
          </button>
        </div>
        <SiteForm
          item={selected}
          site={null}
          busy={busy}
          onSubmit={async (payload) => {
            setBusy(true);
            try {
              onCreated(await configureSite(selected.site_id, payload));
            } catch (e) {
              onError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        />
      </div>
    );
  }

  // 未选定 → 搜索 + 站点列表
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索站点名称 / 地址"
          autoFocus
          className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
        />
        <button
          type="button"
          onClick={onCancel}
          className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
        >
          取消
        </button>
      </div>

      <div className="scroll-thin max-h-64 space-y-1 overflow-y-auto">
        {available.length === 0 ? (
          <p className="px-2 py-6 text-center text-body text-[var(--text-muted)]">
            所有支持的站点都已配置。
          </p>
        ) : filtered.length === 0 ? (
          <p className="px-2 py-6 text-center text-body text-[var(--text-muted)]">
            没有匹配「{query}」的站点。
          </p>
        ) : (
          filtered.map((item) => (
            <button
              key={item.site_id}
              type="button"
              onClick={() => setSelected(item)}
              className="glass-row nav-item w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
            >
              <span className="min-w-0">
                <span className="block truncate text-ui font-medium text-[var(--text)]">
                  {item.display_name}
                </span>
                <span className="block truncate text-caption text-[var(--text-faint)]">
                  {item.base_url}
                </span>
              </span>
              <span className="shrink-0 text-caption text-[var(--text-muted)]">
                {item.supported_auth_types.map((a) => AUTH_TYPE_LABEL[a.auth_type]).join(" / ")}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/* —— 站点行：一行一站，P0 常驻 + P1 条件徽章；点击展开详情 —— */

interface SiteRowProps {
  item: CatalogItem;
  site: ConfiguredSite;
  stats?: SiteSyncStats;
  boost?: SiteBoostStats;
  expanded: boolean;
  onToggle: () => void;
  /** 确保展开（不切换）：菜单里的「编辑授权」需要先展开详情再亮出表单 */
  onOpen: () => void;
  onChanged: (site: ConfiguredSite) => void;
  onDeleted: (siteId: string) => void;
  onError: (message: string) => void;
}

function SiteRow({
  item,
  site,
  stats,
  boost,
  expanded,
  onToggle,
  onOpen,
  onChanged,
  onDeleted,
  onError,
}: SiteRowProps) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  // 授权表单的展开态由行持有：菜单点「编辑授权」时行可能还没展开，需要先展开再亮表单
  const [editingAuth, setEditingAuth] = useState(false);
  // 刷流设置弹窗（预算 + 保留期同窗）：enable=开启前确认，adjust=运行中调整
  const [boostModal, setBoostModal] = useState<"enable" | "adjust" | null>(null);
  const meta = STATUS_META[site.status];
  const failed = site.status === "failed" && !!site.last_error;

  async function guard(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  /** 关闭刷流：二次确认讲清后果（不删数据，只停新增）。 */
  async function disableBoost() {
    const ok = await confirm({
      title: `关闭「${item.display_name}」的自动刷分享率？`,
      bullets: [
        "停止抢该站新发布的免费种子",
        "已在做种的刷流任务全部保留，不删除任何数据",
        "站点索引同步回到正常自适应节奏",
        "重新开启时会再次确认预算与保留期",
      ],
      confirmLabel: "关闭刷流",
    });
    if (!ok) return;
    await guard(async () => onChanged(await setSiteRatioBoost(site.site_id, false)));
  }

  return (
    <div className={site.enabled ? "" : "opacity-60"}>
      {/* 行主体：整行是展开热区。桌面单行；移动端徽章折到第二行（basis-full） */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        className={`group flex min-h-[52px] cursor-pointer flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5 transition-colors hover:bg-white/[0.04] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-white/25 sm:px-5 sm:py-3 ${
          expanded ? "bg-white/[0.045]" : ""
        }`}
      >
        {/* P0：徽标 + 名称 + 状态 */}
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <SiteBadge item={item} />
          {site.protected && (
            <span
              className="flex shrink-0 items-center"
              title="站点保护中：订阅不会自动从该站拉种（下载量归零），手动搜索和下载不受影响"
              aria-label="站点保护中"
            >
              <ShieldIcon className="size-4 text-[var(--info)]" />
            </span>
          )}
          {item.base_url ? (
            <a
              href={item.base_url}
              target="_blank"
              rel="noreferrer"
              title="在新窗口打开站点"
              onClick={(e) => e.stopPropagation()}
              className="truncate text-ui font-semibold text-[var(--text)] underline decoration-transparent underline-offset-4 transition-colors hover:text-white/80 hover:decoration-white/50"
            >
              {item.display_name}
            </a>
          ) : (
            <span className="truncate text-ui font-semibold text-[var(--text)]">
              {item.display_name}
            </span>
          )}
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
            style={{
              background: `color-mix(in oklab, ${meta.color} 12%, transparent)`,
              color: meta.color,
            }}
          >
            <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
            {meta.label}
          </span>
          {!site.enabled && (
            <span className="shrink-0 rounded-full bg-white/[0.08] px-2 py-0.5 text-caption font-medium text-[var(--text-muted)]">
              已停用
            </span>
          )}
        </div>

        {/* P1：条件徽章（异常时错误原因吃掉徽章位——那一刻没有比它更重要的信息）。
            保护状态不占徽章位——站点名前的护盾图标已承担（更紧凑，移动端友好） */}
        {failed ? (
          <p className="order-3 basis-full truncate text-caption text-[var(--danger)] sm:order-none sm:basis-auto sm:max-w-[45%]">
            {site.last_error}
          </p>
        ) : (
          site.boost_enabled && (
            <div className="order-3 basis-full sm:order-none sm:basis-auto">
              <BoostReadout boost={boost} paused={site.boost_paused} />
            </div>
          )
        )}

        {/* 控制区：展开箭头 + 操作菜单 */}
        <div className="flex shrink-0 items-center gap-0.5">
          <span className="flex size-7 items-center justify-center rounded-full text-[var(--text-faint)] transition-colors group-hover:bg-white/[0.08] group-hover:text-[var(--text-muted)]">
            <ChevronDownIcon
              className={`size-4 transition-transform ${expanded ? "rotate-180" : ""}`}
            />
          </span>
          <SiteActionsMenu
            site={site}
            busy={busy}
            onSetEnabled={(enabled) =>
              void guard(async () => onChanged(await setSiteEnabled(site.site_id, enabled)))
            }
            onSetProtected={(next) =>
              void guard(async () => onChanged(await setSiteProtection(site.site_id, next)))
            }
            onEnableBoost={() => setBoostModal("enable")}
            onDisableBoost={() => void disableBoost()}
            onToggleBoostPaused={() =>
              void guard(async () =>
                onChanged(await setSiteBoostPaused(site.site_id, !site.boost_paused)),
              )
            }
            onBoostSettings={() => setBoostModal("adjust")}
            onEditAuth={() => {
              onOpen();
              setEditingAuth(true);
            }}
            onReverify={() =>
              void guard(async () => onChanged(await reverifySite(site.site_id)))
            }
            onDelete={() =>
              void guard(async () => {
                if (
                  !(await confirm({
                    title: `删除「${item.display_name}」的配置？`,
                    description:
                      "该站点将不再参与搜索与订阅投递；在池的刷流任务会转出管理并继续做种。可随时重新接入。",
                    confirmLabel: "删除",
                    tone: "danger",
                  }))
                ) {
                  return;
                }
                await deleteSite(item.site_id);
                onDeleted(item.site_id);
              })
            }
          />
        </div>
      </div>

      {/* 刷流设置弹窗：开启确认与运行中调整共用（预算 + 保留期同窗） */}
      <BoostSettingsModal
        open={boostModal !== null}
        mode={boostModal ?? "adjust"}
        siteName={item.display_name}
        site={site}
        onClose={() => setBoostModal(null)}
        onChanged={onChanged}
      />

      {/* 展开详情：刷流 / 账号 / 索引 / 授权 四段 */}
      {expanded && (
        <SiteDetail
          item={item}
          site={site}
          stats={stats}
          boost={boost}
          busy={busy}
          guard={guard}
          onChanged={onChanged}
          editingAuth={editingAuth}
          onCloseEditAuth={() => setEditingAuth(false)}
        />
      )}
    </div>
  );
}

/* —— 展开详情：P2 信息与刷流设置，移动端统计自动折成两列 —— */

interface SiteDetailProps {
  item: CatalogItem;
  site: ConfiguredSite;
  stats?: SiteSyncStats;
  boost?: SiteBoostStats;
  busy: boolean;
  guard: (fn: () => Promise<void>) => Promise<void>;
  onChanged: (site: ConfiguredSite) => void;
  /** 授权表单展开态由行持有（菜单「编辑授权」可在未展开时触发） */
  editingAuth: boolean;
  onCloseEditAuth: () => void;
}

function SiteDetail({
  item,
  site,
  stats,
  boost,
  busy,
  guard,
  onChanged,
  editingAuth,
  onCloseEditAuth,
}: SiteDetailProps) {
  return (
    <div className="divide-y divide-white/[0.05] border-t border-white/[0.06] bg-white/[0.02] px-4 py-3 sm:px-5">
      {/* ─ 刷流 ─ 只读运行统计；启停与预算在 ⋯ 菜单（带二次确认与预算弹窗） */}
      {site.boost_enabled && (
        <DetailSection label="刷流">
          {site.boost_paused && (
            <p className="text-caption leading-5 text-[var(--warn)]">
              已暂停：在池做种压到极低上传限速，停止汰换与拉新种（任务与数据保留）。
              可在 ⋯ 菜单里「恢复刷流」。
            </p>
          )}
          <StatGrid>
            <DetailStat
              label="已用 / 预算"
              value={`${formatBytes(boost?.used_bytes ?? 0)} / ${formatBytes(site.boost_budget_bytes)}`}
            />
            <DetailStat label="在池做种" value={String(boost?.active_count ?? 0)} />
            <DetailStat
              label="累计上传"
              value={formatBytes(boost?.uploaded_bytes_total ?? 0)}
            />
            <DetailStat
              label="汰换保留期"
              value={site.boost_hold_days > 0 ? `${site.boost_hold_days} 天` : "不保护"}
            />
            {boost && boost.evicted_count > 0 && (
              <DetailStat label="已汰换" value={String(boost.evicted_count)} />
            )}
          </StatGrid>
          {boost && boost.avg_used_bytes_24h > 0 && (
            <p className="text-caption leading-5 text-[var(--text-muted)]">
              近 24 小时：{formatBytes(boost.avg_used_bytes_24h)} 在池种子贡献了{" "}
              {formatBytes(boost.uploaded_bytes_24h)} 上传
              {boost.avg_used_bytes_7d > 0 &&
                ` · 近 7 天：${formatBytes(boost.avg_used_bytes_7d)} 贡献 ${formatBytes(boost.uploaded_bytes_7d)}`}
            </p>
          )}
        </DetailSection>
      )}

      {/* ─ 账号 ─ 两行分组：身份与持有（用户名/等级/做种/魔力）、流量指标（上传/下载/分享率） */}
      {site.profile && (
        <DetailSection label="账号">
          <div
            className="space-y-2.5"
            title={`资料更新于 ${formatRelativeTime(site.profile.fetched_at)}`}
          >
            <StatGrid>
              <DetailStat label="用户名" value={site.profile.username} />
              {site.profile.user_class && (
                <DetailStat label="等级" value={site.profile.user_class} />
              )}
              <DetailStat label="做种" value={String(site.profile.seeding_count)} />
              {site.profile.bonus != null && (
                <DetailStat label="魔力" value={formatCompact(site.profile.bonus)} />
              )}
            </StatGrid>
            <StatGrid>
              <DetailStat label="上传量" value={formatBytes(site.profile.uploaded_bytes)} />
              <DetailStat label="下载量" value={formatBytes(site.profile.downloaded_bytes)} />
              <DetailStat label="分享率" value={formatRatio(site.profile.ratio)} />
            </StatGrid>
          </div>
        </DetailSection>
      )}

      {/* ─ 索引 ─ */}
      {stats && (
        <DetailSection label="索引">
          <StatGrid>
            <DetailStat
              label="已缓存种子"
              value={stats.torrent_count.toLocaleString("zh-CN")}
            />
            <DetailStat label="上次同步" value={formatRelativeTime(stats.last_sync_at)} />
            <DetailStat label="下次同步" value={nextSyncLabel(stats.next_sync_at)} />
            {stats.sync_interval_seconds != null && (
              <DetailStat label="同步间隔" value={formatDuration(stats.sync_interval_seconds)} />
            )}
          </StatGrid>
          {stats.last_error && (
            <p className="text-caption leading-5 text-[#ff6b6b]">上次同步失败：{stats.last_error}</p>
          )}
        </DetailSection>
      )}

      {/* ─ 授权 ─ 编辑入口在 ⋯ 菜单（编辑授权），这里默认只读展示 */}
      <DetailSection label="授权">
        {editingAuth ? (
          <SiteForm
            item={item}
            site={site}
            busy={busy}
            onSubmit={(payload) =>
              guard(async () => {
                onChanged(await updateSite(item.site_id, payload));
                onCloseEditAuth();
              })
            }
          />
        ) : (
          <p className="text-sub leading-6 text-[var(--text-muted)]">
            {AUTH_TYPE_LABEL[site.auth_type]} · 上次检查{" "}
            {formatRelativeTime(site.last_checked_at)}
          </p>
        )}
      </DetailSection>
    </div>
  );
}

/* —— 刷流设置弹窗：预算 + 汰换保留期同窗设置，开启确认与运行中调整共用 —— */

function BoostSettingsModal({
  open,
  mode,
  siteName,
  site,
  onClose,
  onChanged,
}: {
  open: boolean;
  /** enable=开启前的二次确认（讲清将发生什么），adjust=运行中调整 */
  mode: "enable" | "adjust";
  siteName: string;
  site: ConfiguredSite;
  onClose: () => void;
  onChanged: (site: ConfiguredSite) => void;
}) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [budgetGib, setBudgetGib] = useState("");
  const [holdDays, setHoldDays] = useState("");
  // 每次打开按当前生效值重置表单（上次输入不残留）
  useEffect(() => {
    if (!open) return;
    setError(null);
    setBudgetGib(String(Math.round(site.boost_budget_bytes / GIB)));
    setHoldDays(String(site.boost_hold_days));
  }, [open, site.boost_budget_bytes, site.boost_hold_days]);

  async function save() {
    const gib = Math.round(Number(budgetGib.trim()));
    if (!Number.isFinite(gib) || gib < 1) {
      setError("刷流预算必须是不小于 1 的整数（单位 GiB）");
      return;
    }
    const days = Math.round(Number(holdDays.trim()));
    if (!Number.isFinite(days) || days < 0 || days > 30) {
      setError("汰换保留期须是 0～30 之间的整数（天）");
      return;
    }
    // 调小预算的后果不可逆（超出部分连数据删除），保存前二次确认讲清楚
    const currentGib = Math.round(site.boost_budget_bytes / GIB);
    if (mode === "adjust" && gib < currentGib) {
      const ok = await confirm({
        title: `将「${siteName}」的刷流预算从 ${currentGib} GiB 调小到 ${gib} GiB？`,
        bullets: [
          "在池占用超出新预算的部分将被汰换：连同已下载的数据一起删除，且同一种子不会再抢回",
          "按上传效率从低到高删——死种和低效的先走，高效种子最后才会被动",
          "汰换保留期内的任务不受影响，到期后才继续收敛",
          "收敛期间暂停接新的免费种",
        ],
        confirmLabel: "确认调小",
        tone: "danger",
      });
      if (!ok) return;
    }
    setBusy(true);
    setError(null);
    try {
      onChanged(await setSiteRatioBoost(site.site_id, true, gib * GIB, days));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      label={mode === "enable" ? "开启自动刷分享率" : "刷流设置"}
      width="lg"
      topmost
    >
      <div className="space-y-4 p-6">
        <div>
          <h2 className="text-title font-bold text-[var(--text)]">
            {mode === "enable" ? `开启「${siteName}」的自动刷分享率？` : `刷流设置 · ${siteName}`}
          </h2>
          <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
            {mode === "enable"
              ? "开启后将自动抢该站新发布的免费种子做种以提升分享率，占用空间在预算内自动汰换" +
                "（下载完成、入池满保留期且上传效率过低的任务才会被连数据删除），" +
                "该站的索引同步会提速到约 5 分钟一次。"
              : "调小预算会按上传效率从低到高汰换在池任务（连数据删除），直到占用回到新预算内；" +
                "保留期内的任务绝不会被提前删除。"}
          </p>
        </div>

        {error && (
          <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
            {error}
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
          <BoostField
            label="存储预算"
            value={budgetGib}
            unit="GiB"
            min={1}
            disabled={busy}
            onChange={setBudgetGib}
            hint="刷流任务占用磁盘的上限，预算内自动汰换"
          />
          <BoostField
            label="汰换保留期"
            value={holdDays}
            unit="天"
            min={0}
            disabled={busy}
            onChange={setHoldDays}
            hint="H&R 安全垫：有考核的站不小于考核时长；无考核可调 0 自由汰换"
          />
        </div>

        <div className="flex justify-end gap-2.5 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="btn-glass px-4 py-2 text-sub font-medium"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy}
            className="btn-accent rounded-full px-4 py-2 text-sub font-semibold disabled:opacity-60"
          >
            {busy ? "保存中…" : mode === "enable" ? "开启刷流" : "保存"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 带单位后缀与说明的数字输入（弹窗内两枚字段共用）。 */
function BoostField({
  label,
  value,
  unit,
  min,
  disabled,
  onChange,
  hint,
}: {
  label: string;
  value: string;
  unit: string;
  min: number;
  disabled?: boolean;
  onChange: (value: string) => void;
  hint: string;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-caption font-medium text-[var(--text-muted)]">
        {label}
      </label>
      <div className="relative">
        <input
          type="number"
          min={min}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-lg border border-white/10 bg-white/[0.06] px-3 py-2 pr-12 text-ui text-white outline-none transition [appearance:textfield] focus:border-white/25 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
        />
        <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-caption font-medium text-[var(--text-faint)]">
          {unit}
        </span>
      </div>
      <p className="mt-1 text-caption leading-4 text-[var(--text-faint)]">{hint}</p>
    </div>
  );
}

/* —— 下载器拥堵提示条：任务排队 = 活动位满 = 新种提交受限 ——
   数据来自全站共享的下载任务快照（10 秒可见轮询），零额外请求。点击
   直达下载器分区并自动打开对应的「限速与队列」弹窗（?limits=<id>）。
   用 --warn 色：要注意但系统在自己处理（刷流已自动暂停投放）。 */

function QueueCongestionTip() {
  const { tasks } = useDownloadTasks();
  const queued = useMemo(() => tasks.filter((t) => t.state === "queued"), [tasks]);
  if (queued.length === 0) return null;
  // 拥堵的下载器（取排队任务最多的那台作为跳转目标）
  const counts = new Map<number, { name: string; count: number }>();
  for (const task of queued) {
    if (task.downloader_id == null) continue;
    const entry = counts.get(task.downloader_id) ?? {
      name: task.downloader_name ?? `#${task.downloader_id}`,
      count: 0,
    };
    entry.count += 1;
    counts.set(task.downloader_id, entry);
  }
  const worst = [...counts.entries()].sort((a, b) => b[1].count - a[1].count)[0];
  if (!worst) return null;
  const [downloaderId, { name }] = worst;

  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border px-4 py-3 text-sub"
      style={{
        borderColor: "color-mix(in oklab, var(--warn) 30%, transparent)",
        background: "color-mix(in oklab, var(--warn) 10%, transparent)",
        color: "var(--warn)",
      }}
    >
      <span className="min-w-0 flex-1">
        下载器「{name}」有 {queued.length} 个任务在排队——活动任务位已满，新种子提交受限，
        刷流已自动暂停投放。建议调大「最大活动种子数」等队列上限。
      </span>
      <Link
        href={`/settings/downloaders?limits=${downloaderId}` as Route}
        className="shrink-0 rounded-full border px-3 py-1 font-medium transition hover:bg-[color-mix(in_oklab,var(--warn)_18%,transparent)]"
        style={{ borderColor: "color-mix(in oklab, var(--warn) 40%, transparent)" }}
      >
        去调整
      </Link>
    </div>
  );
}

/* —— 刷流行级读数：只回答「刷流在产出吗」——近 24h 上传量一个数字 ——
   预算用量不上行：池子的稳态就是被填满后汰换周转，「有多满」既非好消息也
   非坏消息，没有行级信号价值（详情展开区仍有完整的已用/预算）。刚开启还
   没有产出数据时退回显示已用量，让用户看到引擎确实在动。数字穿文本色，
   「刷流」小字标签用 accent 特性色标识身份，不与状态徽章的绿色混淆。 */

function BoostReadout({ boost, paused }: { boost?: SiteBoostStats; paused?: boolean }) {
  const up24 = boost?.uploaded_bytes_24h ?? 0;
  const used = boost?.used_bytes ?? 0;
  // 暂停态吃掉产出读数：那一刻「为什么没产出」比「产出多少」更重要
  if (paused) {
    return (
      <div
        className="flex min-w-0 items-center gap-1.5"
        title="刷流已暂停：做种压到极低上传限速、停止汰换与拉新种，任务与数据保留；在 ⋯ 菜单里可随时恢复"
      >
        <span className="shrink-0 text-caption font-medium text-[var(--accent)]">刷流</span>
        <span className="truncate text-caption font-medium text-[var(--warn)]">
          已暂停 · 做种限速让出上行
        </span>
      </div>
    );
  }
  return (
    <div
      className="flex min-w-0 items-center gap-1.5"
      title="自动刷分享率运行中：近 24 小时的上传贡献（完整统计见展开详情）"
    >
      <span className="shrink-0 text-caption font-medium text-[var(--accent)]">刷流</span>
      <span className="truncate text-caption text-[var(--text-muted)]">
        {up24 > 0
          ? `24h ↑${formatBytes(up24)}`
          : used > 0
            ? `已用 ${formatBytes(used)}`
            : "等待首个免费种"}
      </span>
    </div>
  );
}

/* —— 站点徽标：优先取站点真实 favicon（域名 + /favicon.ico），失败回落首字母 ——
   经后端 /images/proxy 统一图片代理回源（cachedImageUrl 收口）：favicon 也是
   站点流量，必须走 movieclaw_net 统一出口受代理路由与网络策略管控，且服务端
   落盘缓存后浏览器不再反复触达站点。刻意不走 Google/DuckDuckGo 的 favicon
   聚合服务：目标用户网络环境里那些域名普遍不可达。 */

/** favicon 失败负缓存的 TTL：站点 favicon 挂了不该每次进页面都重试一发。
 *  成功端有浏览器一年 immutable 缓存，失败端靠 localStorage 记一天，
 *  一天后自动重试（站点修好后最多一天恢复图标）。 */
const _FAVICON_FAIL_TTL_MS = 24 * 60 * 60 * 1000;

function faviconFailKey(origin: string): string {
  return `mc:favicon-fail:${origin}`;
}

function faviconRecentlyFailed(origin: string): boolean {
  try {
    const at = Number(localStorage.getItem(faviconFailKey(origin)));
    return Number.isFinite(at) && Date.now() - at < _FAVICON_FAIL_TTL_MS;
  } catch {
    return false; // 隐私模式等 localStorage 不可用：退化为每次尝试
  }
}

function rememberFaviconFailure(origin: string): void {
  try {
    localStorage.setItem(faviconFailKey(origin), String(Date.now()));
  } catch {
    /* 记不住就算了，只损失负缓存 */
  }
}

function SiteBadge({ item }: { item: CatalogItem }) {
  const origin = useMemo(() => {
    if (!item.base_url) return null;
    try {
      return new URL(item.base_url).origin;
    } catch {
      return null;
    }
  }, [item.base_url]);
  // 初始态就吃负缓存：一天内失败过的站直接出字母徽标，连请求都不发
  const [failed, setFailed] = useState(() => (origin ? faviconRecentlyFailed(origin) : false));

  if (!origin || failed) {
    return (
      <span className="icon-chip size-8 shrink-0 !rounded-lg text-sub font-semibold">
        {item.display_name.charAt(0).toUpperCase()}
      </span>
    );
  }
  return (
    <span className="flex size-8 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-white/[0.08] bg-white/[0.04]">
      {/* 经统一图片代理的外站 favicon，不经 next/image 优化管道 */}
      <img
        src={cachedImageUrl(`${origin}/favicon.ico`)}
        alt=""
        loading="lazy"
        className="size-5"
        onError={() => {
          rememberFaviconFailure(origin);
          setFailed(true);
        }}
      />
    </span>
  );
}

/**
 * 详情分段：桌面把段标签抽成左侧固定列（内容区左缘因此对齐，
 * 各段的统计格子在纵向也成列），窄屏回落成「标签在上、内容在下」。
 */
function DetailSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="grid gap-1.5 py-3 first:pt-1 last:pb-1 sm:grid-cols-[46px_minmax(0,1fr)] sm:gap-x-4 sm:gap-y-0">
      <p className="text-micro font-medium tracking-wide text-[var(--text-faint)] sm:pt-2">
        {label}
      </p>
      <div className="min-w-0 space-y-2">{children}</div>
    </section>
  );
}

/**
 * 统计区容器：等宽列（2 → 3 → 4 列）。
 * 刻意不给底色和描边——层级全靠列对齐与字重/明暗分层撑住，
 * 配置页要的是克制，加一圈卡片底色反而把「轻」丢了。
 */
function StatGrid({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
      {children}
    </div>
  );
}

/** 单个统计：淡色小标签在上、数值在下；数值走等宽数字，纵向列里数位对齐 */
function DetailStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="truncate text-micro text-[var(--text-faint)]">{label}</p>
      {/* 移动端降一档字号：窄屏两列下「998 GB / 1000 GB」这类长值按 text-ui 会被截断 */}
      <p className="mt-0.5 truncate text-ui font-semibold tabular-nums text-[var(--text)] max-sm:text-sub">
        {value}
      </p>
    </div>
  );
}

/** 「下次同步」文案：null（立即到期）或时刻已过（等待 tick 扫描）都显示「即将开始」，
 *  避免出现"下次同步：3 分钟前"这种矛盾表述。 */
function nextSyncLabel(iso: string | null): string {
  if (!iso || new Date(iso).getTime() <= Date.now()) return "即将开始";
  return formatRelativeTime(iso);
}

/* —— 站点操作折叠菜单：启停 / 保护 / 刷流 / 编辑授权 / 重验 / 删除 全部收口于此 —— */

interface SiteActionsMenuProps {
  site: ConfiguredSite;
  busy: boolean;
  onSetEnabled: (enabled: boolean) => void;
  onSetProtected: (next: boolean) => void;
  onEnableBoost: () => void;
  onDisableBoost: () => void;
  onToggleBoostPaused: () => void;
  onBoostSettings: () => void;
  onEditAuth: () => void;
  onReverify: () => void;
  onDelete: () => void;
}

function SiteActionsMenu({
  site,
  busy,
  onSetEnabled,
  onSetProtected,
  onEnableBoost,
  onDisableBoost,
  onToggleBoostPaused,
  onBoostSettings,
  onEditAuth,
  onReverify,
  onDelete,
}: SiteActionsMenuProps) {
  // Radix DropdownMenu：菜单渲染进 body Portal 并做碰撞检测；
  // 菜单项加大内边距保证移动端触控目标
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2.5 text-sub font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const canReverify = !IN_PROGRESS.includes(site.status);

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="站点操作"
          onClick={(e) => e.stopPropagation()}
          className="glass-row !w-auto p-2 data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)]"
        >
          <MoreIcon className="size-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[10rem] p-1"
          onClick={(e) => e.stopPropagation()}
        >
          <DropdownMenu.Item
            onSelect={() => onSetEnabled(!site.enabled)}
            disabled={busy}
            className={itemClass}
          >
            {site.enabled ? "停用站点" : "启用站点"}
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={() => onSetProtected(!site.protected)}
            disabled={busy}
            className={itemClass}
          >
            {site.protected ? "取消保护" : "开启保护"}
          </DropdownMenu.Item>
          {/* 刷流启停都带二次确认（开启含预算设置）——onSelect 后弹的是
              feedback 层的对话框，菜单自身正常关闭即可 */}
          <DropdownMenu.Item
            onSelect={site.boost_enabled ? onDisableBoost : onEnableBoost}
            disabled={busy}
            className={itemClass}
          >
            {site.boost_enabled ? "关闭刷流…" : "开启刷流…"}
          </DropdownMenu.Item>
          {/* 暂停/恢复：临时给前台流量（看视频等）让出上行——做种限速 +
              停止汰换拉新，任务保留，随时无损恢复，故不设二次确认 */}
          {site.boost_enabled && (
            <DropdownMenu.Item
              onSelect={onToggleBoostPaused}
              disabled={busy}
              className={itemClass}
            >
              {site.boost_paused ? "恢复刷流" : "暂停刷流"}
            </DropdownMenu.Item>
          )}
          {site.boost_enabled && (
            <DropdownMenu.Item onSelect={onBoostSettings} disabled={busy} className={itemClass}>
              刷流设置…
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={onEditAuth} disabled={busy} className={itemClass}>
            编辑授权
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onReverify}
            disabled={busy || !canReverify}
            className={itemClass}
          >
            重新验证
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onDelete}
            disabled={busy}
            className={`${itemClass} !text-[#ff6b6b] data-[highlighted]:!bg-[#ff6b6b]/10`}
          >
            删除配置
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/* —— 授权表单：根据所选授权类型渲染必填字段 —— */

interface SiteFormProps {
  item: CatalogItem;
  site: ConfiguredSite | null;
  busy: boolean;
  onSubmit: (payload: SiteConfigPayload) => void;
}

function SiteForm({ item, site, busy, onSubmit }: SiteFormProps) {
  const options = item.supported_auth_types;
  // 默认选中：已配置的沿用其类型，否则取第一个支持项
  const [authType, setAuthType] = useState<SiteAuthType>(
    site?.auth_type ?? options[0]?.auth_type ?? "cookie",
  );
  // 各字段值。编辑时出于安全后端不回传敏感值，故一律留空，需用户重新填写。
  const [values, setValues] = useState<Record<string, string>>({});

  const current = options.find((o) => o.auth_type === authType) ?? options[0];
  const fields = current?.required_fields ?? [];

  const canSubmit = fields.length > 0 && fields.every((f) => values[f]?.trim());

  function submit() {
    const payload: SiteConfigPayload = { auth_type: authType, enabled: site?.enabled ?? true };
    for (const f of fields) {
      (payload as unknown as Record<string, unknown>)[f] = values[f]?.trim() ?? "";
    }
    onSubmit(payload);
  }

  return (
    <div className="space-y-4">
      {/* 授权类型选择（多于一种时才展示） */}
      {options.length > 1 && (
        <div>
          <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">
            授权方式
          </label>
          <div className="flex flex-wrap gap-2">
            {options.map((opt: AuthTypeRequirement) => (
              <button
                key={opt.auth_type}
                type="button"
                onClick={() => setAuthType(opt.auth_type)}
                data-active={authType === opt.auth_type}
                className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
              >
                {AUTH_TYPE_LABEL[opt.auth_type]}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 必填字段 */}
      {fields.map((field) => {
        const fm = FIELD_META[field] ?? { label: field, kind: "text" as const };
        return (
          <div key={field}>
            <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">
              {fm.label}
            </label>
            {/* Cookie 恰是插件的用武之地：就地提一句，不打断手动粘贴的用户 */}
            {field === "cookie" && (
              <p className="mb-1.5 text-caption text-[var(--text-faint)]">
                手动粘贴的 Cookie 过期后需重填；推荐用本页下方的 MovieClaw 浏览器插件自动同步。
              </p>
            )}
            {fm.kind === "textarea" ? (
              <textarea
                value={values[field] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
                rows={3}
                autoComplete="off"
                placeholder={site ? "出于安全，请重新填写" : ""}
                className="scroll-thin w-full resize-none rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
              />
            ) : (
              <input
                type={fm.kind}
                value={values[field] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
                placeholder={site ? "出于安全，请重新填写" : ""}
                // Chrome 对 password 字段会无视 "off" 仍弹出已存密码，须用 "new-password" 抑制
                autoComplete={fm.kind === "password" ? "new-password" : "off"}
                className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
              />
            )}
          </div>
        );
      })}

      <div className="flex items-center justify-end gap-3 pt-1">
        <button
          type="button"
          onClick={submit}
          disabled={busy || !canSubmit}
          className="btn-accent rounded-full px-4.5 py-2 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : site ? "保存并重新验证" : "保存并验证"}
        </button>
      </div>
    </div>
  );
}
