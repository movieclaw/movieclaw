/**
 * 骨架阶段的占位数据。
 * 后续接入 FastAPI 后端时，这里的静态数组会替换为接口返回（见 lib/api/）。
 * 现在只为把布局与交互跑通，字段尽量贴近真实产品形态。
 */
import type { ComponentType, SVGProps } from "react";
import {
  ActivityIcon,
  BookmarkIcon,
  ChatIcon,
  DeviceIcon,
  DownloadIcon,
  FilmIcon,
  FolderIcon,
  GearIcon,
  GlobeIcon,
  PaletteIcon,
  PhotoIcon,
  PlayIcon,
  SendIcon,
  ServerIcon,
  ShieldIcon,
  SparkIcon,
  TerminalIcon,
  TvIcon,
  UserIcon,
} from "@/components/icons";

type Icon = ComponentType<SVGProps<SVGSVGElement>>;

/** 「探索」分组里的操作按钮 */
export interface ExploreItem {
  id: string;
  label: string;
  icon: Icon;
}

export const exploreItems: ExploreItem[] = [
  { id: "explore-movies", label: "发现电影", icon: FilmIcon },
  { id: "explore-tv", label: "发现剧集", icon: TvIcon },
];

/** 最近会话（类 Codex / ChatGPT 的会话列表） */
export type RunStatus = "running" | "done" | "failed";

export interface RecentRun {
  id: string;
  title: string;
  preview: string;
  status: RunStatus;
  time: string;
}

export const recentRuns: RecentRun[] = [
  {
    id: "run-1",
    title: "追踪《沙丘 2》4K 资源",
    preview: "已在 3 个站点命中，正在校验做种健康度…",
    status: "running",
    time: "刚刚",
  },
  {
    id: "run-2",
    title: "订阅《幕府将军》全季",
    preview: "第 10 集已入库，等待字幕匹配。",
    status: "done",
    time: "12 分钟前",
  },
  {
    id: "run-3",
    title: "补齐《绝命毒师》缺失剧集",
    preview: "S02E07 未找到合适资源。",
    status: "failed",
    time: "1 小时前",
  },
  {
    id: "run-4",
    title: "每周热门电影自动巡检",
    preview: "已生成 8 条候选，全部确认入库。",
    status: "done",
    time: "昨天",
  },
  {
    id: "run-5",
    title: "订阅《怪奇物语》最终季",
    preview: "全季 8 集已入库，字幕匹配完成。",
    status: "done",
    time: "2 天前",
  },
  {
    id: "run-6",
    title: "清理低做种历史种子",
    preview: "已归档 23 个种子，释放 180GB。",
    status: "done",
    time: "3 天前",
  },
];

export const runStatusMeta: Record<RunStatus, { label: string; color: string }> = {
  running: { label: "运行中", color: "#6aa7ff" },
  done: { label: "已完成", color: "#4ade80" },
  failed: { label: "失败", color: "#ff6b6b" },
};

/** 设置页的分区（进入设置后替换左侧菜单） */
export interface SettingsSection {
  id: string;
  label: string;
  description: string;
  icon: Icon;
}

/** 设置分区的分组：侧栏按组渲染小节标题，组内顺序即展示顺序 */
export interface SettingsSectionGroup {
  label: string;
  items: SettingsSection[];
}

/**
 * 分组标准统一为「回答用户什么问题」：账号（我是谁）→ 成员与设备（谁能进来）→
 * 资源与下载（内容怎么来）→ 媒体库（内容长什么样、怎么看）→ 通知与集成
 * （系统怎么告诉外界）→ 系统（运维）。文件到手之前的事归「资源与下载」，
 * 到手之后归「媒体库」，这条边界决定了刮削、播放的归属。
 */
export const settingsSectionGroups: SettingsSectionGroup[] = [
  {
    // 概览不属于任何分组、不设组标题（侧栏对空标题不渲染小节头）：
    // 它是管理员进设置的落地页——未配齐必要件时是开局清单（哪些必须配、
    // 下一步做什么），配齐后是链路体检与更新提示，让问题主动找人，
    // 而不是让新手在十几个分区里翻。
    label: "",
    items: [
      {
        id: "overview",
        label: "概览",
        description: "配置状态一览：缺什么、有什么问题、下一步做什么",
        icon: ActivityIcon,
      },
    ],
  },
  {
    label: "账号",
    items: [
      { id: "profile", label: "个人信息", description: "头像、昵称与登录密码", icon: UserIcon },
      { id: "appearance", label: "外观", description: "首页背景与界面质感", icon: PaletteIcon },
    ],
  },
  {
    // 成员与设备都是访问控制——一个管人、一个管命令行与转码 Worker。
    // 成员权限（能力开关 + 库/站点白名单）的写入口唯一在这里，库页面零改动
    // （docs/design/member-management.md §3.9.1）。
    label: "成员与设备",
    items: [
      { id: "members", label: "成员", description: "家庭成员账号、能力开关与可见范围", icon: ShieldIcon },
      { id: "devices", label: "设备", description: "命令行与转码 Worker 的接入审批和吊销", icon: DeviceIcon },
    ],
  },
  {
    // 组内按资源接入链路排序：订阅规则（消费整条链路）→ 站点（含插件
    // Cookie 同步）→ 下载器 → 自动入库（下载完成的收尾）。
    label: "资源与下载",
    items: [
      { id: "subscription", label: "订阅规则", description: "订阅规则组与投递模拟预演", icon: BookmarkIcon },
      { id: "sites", label: "资源站点", description: "站点接入与鉴权、搜索分类、插件 Cookie 同步", icon: ServerIcon },
      { id: "downloaders", label: "下载器", description: "qBittorrent / Transmission 接入", icon: DownloadIcon },
      { id: "import-watch", label: "自动入库", description: "监听下载目录，下载完成后自动整理进媒体库", icon: FolderIcon },
    ],
  },
  {
    // 刮削的每库级覆盖在「编辑库 → 刮削设置」里，这里是全局默认口味；
    // 「播放」按功能命名而非按当前唯一的实现叫「远程转码」——给未来的
    // 转码策略、字幕偏好等播放域设置留好家。
    label: "媒体库",
    items: [
      { id: "scrape", label: "刮削与整理", description: "海报、简介、命名与目录整理的全局默认", icon: PhotoIcon },
      { id: "playback", label: "播放", description: "远程转码与播放体验", icon: PlayIcon },
    ],
  },
  {
    // 推人（消息推送）、推服务（Webhook）、AI 供应商接入，都是对外集成；
    // AI 模型紧挨着它最大的消费方（推送里的 AI 对话）。
    label: "通知与集成",
    items: [
      { id: "im-push", label: "消息推送", description: "微信 / Telegram / Discord 推送与 AI 对话", icon: ChatIcon },
      { id: "webhook", label: "Webhook", description: "向外部服务推送播放、收藏等事件", icon: SendIcon },
      { id: "llm", label: "AI 模型", description: "接入 AI 服务，用于对话助手与智能识别", icon: SparkIcon },
    ],
  },
  {
    // 装完配一次/出问题才碰的低频运维项，沉底
    label: "系统",
    items: [
      { id: "app", label: "更新与维护", description: "版本更新与应用重启", icon: GearIcon },
      { id: "network", label: "网络", description: "代理、镜像与外部访问地址，解决 TMDB 等不可达", icon: GlobeIcon },
      { id: "logs", label: "系统日志", description: "后端运行日志，按天存档", icon: TerminalIcon },
    ],
  },
];

/** 扁平分区列表：路由校验、默认分区等按 id 消费的场景继续用它 */
export const settingsSections: SettingsSection[] = settingsSectionGroups.flatMap(
  (group) => group.items,
);

/** 成员可见的设置分区（其余分区后端一律 403，前端不给入口）。 */
const MEMBER_SECTION_IDS = new Set(["profile", "appearance"]);

/**
 * 按角色过滤设置分区分组：管理员全量；成员只剩「账号」组的个人分区
 * （概览呈现的是全局配置健康，属管理员视角，成员不可见）。
 * 这只是界面裁剪——安全边界在后端的 require_admin / 守护测试。
 */
export function settingsSectionGroupsFor(
  role: "admin" | "member",
): SettingsSectionGroup[] {
  if (role === "admin") return settingsSectionGroups;
  return settingsSectionGroups
    .map((group) => ({
      ...group,
      items: group.items.filter((s) => MEMBER_SECTION_IDS.has(s.id)),
    }))
    .filter((group) => group.items.length > 0);
}
