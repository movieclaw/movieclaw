"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { LiquidGlassButton } from "@/vendor/liquid-glass";

import { AppConfigSection } from "@/components/app-config-section";
import { AppUpdateDot, usePendingUpdate } from "@/components/app-update-entry";
import { AppUpdateSection } from "@/components/app-update-section";
import { AvatarBadge } from "@/components/avatar-badge";
import { DevicesSection } from "@/components/devices-section";
import { DownloaderConfigSection } from "@/components/downloader-config-section";
import { useConfirm } from "@/components/feedback";
import { ImportWatchSection } from "@/components/import-watch-section";
import { MembersSection } from "@/components/members-section";
import { LlmConfigSection } from "@/components/llm-config-section";
import { ImPushSection } from "@/components/im-push-section";
import { NetworkConfigSection } from "@/components/network-config-section";
import { RemoteTranscodeSection } from "@/components/remote-transcode-section";
import { ScrapeSettingsSection } from "@/components/scrape-settings-section";
import { SiteConfigSection, SitesSectionSubtitle } from "@/components/site-config-section";
import { SubscriptionSettingsSection } from "@/components/subscription-settings-section";
import { SystemLogsSection } from "@/components/system-logs-section";
import { WebhookSection } from "@/components/webhook-section";
import { GlassPanel } from "@/components/glass-panel";
import {
  ArrowLeftIcon,
  CheckIcon,
  ChevronDownIcon,
  GripIcon,
  PlusIcon,
  XIcon,
} from "@/components/icons";
import { useVisibleNavItems } from "@/components/sidebar";
import { fileToCompressedJpeg, useBackdrop } from "@/lib/backdrop";
import { BACKDROP, sidebarGlass } from "@/lib/glass";
import { changePassword, updateProfile, uploadAvatar } from "@/lib/api/auth";
import { DEFAULT_UI_PREFS } from "@/lib/api/ui";
import { HttpError } from "@/lib/http";
import { useSession } from "@/lib/session";
import { applyNavOrder, mergeNavOrder, sameNavOrder } from "@/lib/sidebar-nav";
import { settingsSectionGroupsFor, settingsSections } from "@/lib/mock-data";
import { useUiPrefs } from "@/lib/ui-prefs";

/**
 * 设置模式的左栏：替换掉工作台侧边栏。
 * 顶部是「返回」按钮（回到主区域 / 工作台），下面是设置分区列表。
 */
export interface SettingsSidebarProps {
  active: string;
  onSelect: (id: string) => void;
  onBack: () => void;
}

export function SettingsSidebar({ active, onSelect, onBack }: SettingsSidebarProps) {
  const { backdrop } = useBackdrop();
  // 成员只看到「通用」组（个人信息/外观）；管理分区后端一律 403，前端不给入口
  const { session } = useSession();
  const sectionGroups = settingsSectionGroupsFor(session.role);
  // 与工作台侧栏共用同一份用户偏好（透明度/明暗）；外观分区拖动滑杆时，
  // 这块面板就是实时预览的对象。
  const { prefs } = useUiPrefs();
  const glass = sidebarGlass(prefs.sidebar);
  // 有可用更新时给「应用」分区行点一颗小蓝点：从别的入口进了设置，也能
  // 一眼看出更新在哪一栏（与侧栏更新入口同一份快照数据）
  const pendingUpdate = usePendingUpdate(session.role !== "member");
  return (
    <GlassPanel
      backgroundImage={backdrop}
      variant={glass.variant}
      className="panel--sidebar h-full"
      contentClassName="flex h-full flex-col"
      sampleBackground={glass.sampleBackground}
      settings={glass.settings}
      hairlineAlpha={glass.hairlineAlpha}
      fallbackAlpha={glass.fallbackAlpha}
    >
      {/* 顶部：返回按钮（玻璃胶囊，轮廓明确的「可点」入口） */}
      <div className="px-3 pb-1 pt-3.5">
        <button
          type="button"
          onClick={onBack}
          className="btn-glass px-3.5 py-1.5 text-sub font-medium text-[var(--text-muted)] hover:text-[var(--text)]"
        >
          <ArrowLeftIcon className="size-4" />
          <span>返回工作台</span>
        </button>
      </div>

      <div className="px-4 pb-4 pt-4">
        <h2 className="text-sheen text-title-lg font-semibold tracking-tight">设置</h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">管理账号与工作台偏好</p>
      </div>

      {/* 分区列表：标准 SaaS 设置菜单——紧凑单行（小图标内联 + 标签），
          描述只在右侧面板头部展示，选中态仍用亮胶囊表达 */}
      <nav className="scroll-thin flex-1 space-y-4 overflow-y-auto px-3 pb-4">
        {sectionGroups.map((group) => (
          <div key={group.label}>
            <h3 className="group-label mb-1.5 px-2">{group.label}</h3>
            <div className="space-y-0.5">
              {group.items.map((section) => {
                const Icon = section.icon;
                return (
                  <button
                    key={section.id}
                    type="button"
                    data-active={active === section.id}
                    onClick={() => onSelect(section.id)}
                    className="glass-row nav-item gap-2.5 px-2.5 py-[7px]"
                  >
                    <Icon className="size-4 shrink-0" />
                    <span className="flex-1 truncate text-ui font-medium">
                      {section.label}
                    </span>
                    {section.id === "app" && pendingUpdate && <AppUpdateDot />}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </GlassPanel>
  );
}

/**
 * 设置模式的右区：展示当前分区的内容（骨架）。
 * 返回主页统一交给左栏顶部的「返回」入口，这里的头部只做面包屑标题，不再放返回按钮。
 */
export interface SettingsPanelProps {
  active: string;
}

export function SettingsPanel({ active }: SettingsPanelProps) {
  // 成员直达管理分区地址（书签/手输 URL）时回退到个人信息——界面兜底，
  // 真正的安全边界在后端 403
  const { session } = useSession();
  const allowed = settingsSectionGroupsFor(session.role).flatMap((g) => g.items);
  const section =
    allowed.find((s) => s.id === active) ?? allowed[0] ?? settingsSections[0];
  const Icon = section.icon;

  return (
    // 无外框、无玻璃卡片：内容直接铺在全屏深色蒙版（.page-scrim）之上，
    // 背景透明，让蒙版透上来。沉浸式深色底，不再有圆角/描边/透出雪原的大卡片。
    // 头部与内容同列（同一 max-w 容器内），避免「标题贴左上、内容居中」的割裂感。
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      {/* 分区按信息密度给宽度：日志行最密（4xl）；资源站点一行要放
          名称 + 状态 + 刷流读数 + 操作，展开后还有成排统计，2xl 太挤（3xl）；
          下载器展开后是地址/目录长值 + 路径映射对照表，同给 3xl；
          其余表单类分区维持 2xl 的舒适阅读宽度 */}
      <div
        className={`mx-auto w-full px-6 pb-20 pt-12 max-md:px-4 max-md:pb-12 max-md:pt-6 ${
          section.id === "logs"
            ? "max-w-4xl"
            : section.id === "sites" || section.id === "downloaders"
              ? "max-w-3xl"
              : "max-w-2xl"
        }`}
      >
        <header className="flex items-center gap-4">
          <span className="icon-chip size-12 !rounded-2xl">
            <Icon className="size-[22px]" />
          </span>
          <div className="min-w-0">
            {/* 实色 + 暗投影（不用 text-sheen 渐变裁切——全站蒙版默认轻档、
                背景大图透上来时，半透明渐变字压在亮背景上会发灰，实色白字最稳） */}
            <h1 className="text-on-image text-[22px] font-semibold tracking-tight text-[var(--text)] max-md:text-title-lg">
              {section.label}
            </h1>
            <p className="text-on-image mt-0.5 text-ui text-[var(--text-muted)]">
              {/* 资源站点分区的副标题是活的：有站点时显示健康统计，无站点回落介绍 */}
              {section.id === "sites" ? (
                <SitesSectionSubtitle fallback={section.description} />
              ) : (
                section.description
              )}
            </p>
          </div>
        </header>

        {/* 发丝分隔线：左亮右隐的渐变，呼应玻璃边缘的受光 */}
        <div className="mb-8 mt-7 h-px bg-gradient-to-r from-white/[0.14] via-white/[0.06] to-transparent" />

        {section.id === "profile" ? (
          <ProfileSection />
        ) : section.id === "appearance" ? (
          <AppearanceSection />
        ) : section.id === "subscription" ? (
          <SubscriptionSettingsSection />
        ) : section.id === "sites" ? (
          <SiteConfigSection />
        ) : section.id === "downloaders" ? (
          <DownloaderConfigSection />
        ) : section.id === "import-watch" ? (
          <ImportWatchSection />
        ) : section.id === "scrape" ? (
          <ScrapeSettingsSection />
        ) : section.id === "llm" ? (
          <LlmConfigSection />
        ) : section.id === "im-push" ? (
          <ImPushSection />
        ) : section.id === "members" ? (
          <MembersSection />
        ) : section.id === "devices" ? (
          <DevicesSection />
        ) : section.id === "app" ? (
          <AppSection />
        ) : section.id === "webhook" ? (
          <WebhookSection />
        ) : section.id === "network" ? (
          <NetworkConfigSection />
        ) : section.id === "logs" ? (
          <SystemLogsSection />
        ) : (
          <GenericSection sectionId={section.id} />
        )}
      </div>
    </div>
  );
}

/**
 * 分组容器：小号大写分组标签 + 一张玻璃卡片。
 * 卡片内的行由使用方提供，多行时配合 divide-y 呈现 macOS 设置式的字段组。
 */
function SettingsGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="group-label mb-2.5 px-1">{label}</h3>
      {children}
    </section>
  );
}

/* —— 个人信息分区（真实账号数据，来自登录会话） —— */
function ProfileSection() {
  const { session, setSession } = useSession();
  const avatarInputRef = useRef<HTMLInputElement>(null);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);

  /** 选图后：前端压到 512px JPEG 再上传（头像不需要大图），成功即同步会话
   *  上下文——avatar_url 带新版本号，侧栏用户菜单等所有展示处立即换新图。 */
  const handleAvatarPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // 清空 value：否则再次选择同一张图不会触发 change 事件。
    e.target.value = "";
    if (!file) return;
    setAvatarBusy(true);
    setAvatarError(null);
    try {
      const blob = await fileToCompressedJpeg(file, 512);
      setSession(await uploadAvatar(blob));
    } catch (err) {
      setAvatarError(err instanceof Error ? err.message : "上传失败，请重试");
    } finally {
      setAvatarBusy(false);
    }
  };

  return (
    <div className="space-y-8">
      {/* 账号总览卡：大圆形头像（点击上传 / 替换）+ 昵称 / 用户名 / 身份徽章 */}
      <div className="css-glass flex items-center gap-5 !rounded-2xl p-6">
        <button
          type="button"
          onClick={() => avatarInputRef.current?.click()}
          disabled={avatarBusy}
          aria-label="上传头像"
          title="点击更换头像"
          className="group relative shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
        >
          <AvatarBadge
            nickname={session.nickname}
            avatarUrl={session.avatar_url}
            className="size-[72px] text-2xl"
          />
          {/* hover / 上传中：圆形遮罩浮出提示，暗示头像可点击更换 */}
          <span
            className={`absolute inset-0 flex items-center justify-center rounded-full bg-black/55 text-caption font-semibold text-white transition-opacity ${
              avatarBusy ? "opacity-100" : "touch-reveal opacity-0 group-hover:opacity-100"
            }`}
          >
            {avatarBusy ? "上传中…" : "更换"}
          </span>
        </button>
        <input
          ref={avatarInputRef}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => void handleAvatarPick(e)}
        />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <p className="text-xl font-semibold tracking-tight">{session.nickname}</p>
            <span className="rounded-full border border-white/[0.12] bg-[var(--accent-soft)] px-2.5 py-0.5 text-caption font-semibold text-[var(--accent)]">
              {session.role === "member" ? "成员" : "超级管理员"}
            </span>
          </div>
          <p className="mt-1 text-body text-[var(--text-muted)]">@{session.username}</p>
          {avatarError && (
            <p className="mt-1.5 text-sub text-[var(--danger)]">{avatarError}</p>
          )}
        </div>
      </div>

      {/* 字段组：合并进一张卡片，行间发丝分隔（macOS 设置式），不再是散落的孤立圆角块 */}
      <SettingsGroup label="账号信息">
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          <NicknameRow />
          <FieldRow label="用户名" value={session.username} hint="登录凭证，不可修改" />
        </div>
      </SettingsGroup>

      <SettingsGroup label="安全">
        <ChangePasswordCard />
      </SettingsGroup>
    </div>
  );
}

/** 只读字段行（可附加说明文字）。 */
function FieldRow({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-center justify-between gap-4 px-5 py-4 first:rounded-t-2xl last:rounded-b-2xl">
      <div>
        <p className="text-body font-medium text-[var(--text)]">{label}</p>
        {hint && <p className="mt-0.5 text-caption text-[var(--text-faint)]">{hint}</p>}
      </div>
      <span className="text-body text-[var(--text-muted)]">{value}</span>
    </div>
  );
}

/** 昵称行：点「编辑」原地展开输入框，保存后同步会话上下文（侧栏立即更新）。 */
function NicknameRow() {
  const { session, setSession } = useSession();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEdit = () => {
    setDraft(session.nickname);
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    const nickname = draft.trim();
    if (!nickname) {
      setError("昵称不能为空");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setSession(await updateProfile(nickname));
      setEditing(false);
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="px-5 py-4 first:rounded-t-2xl last:rounded-b-2xl">
      <div className="flex items-center justify-between gap-4">
        <p className="text-body font-medium text-[var(--text)]">昵称</p>
        {editing ? (
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && save()}
              maxLength={32}
              autoFocus
              className="w-44 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
            <button
              type="button"
              onClick={save}
              disabled={busy}
              className="btn-accent rounded-full px-3.5 py-1.5 text-sub font-semibold disabled:opacity-40"
            >
              {busy ? "保存中…" : "保存"}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              disabled={busy}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              取消
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-3">
            <span className="text-body text-[var(--text-muted)]">{session.nickname}</span>
            <button type="button" onClick={startEdit} className="btn-glass px-3 py-1 text-sub font-medium">
              编辑
            </button>
          </div>
        )}
      </div>
      {error && <p className="mt-2 text-right text-sub text-[var(--danger)]">{error}</p>}
    </div>
  );
}

/** 修改密码卡片：改密成功后其他设备的会话全部失效，本会话自动续期。 */
function ChangePasswordCard() {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async () => {
    if (newPassword.length < 8) {
      setError("新密码至少 8 位");
      return;
    }
    if (newPassword !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await changePassword(oldPassword, newPassword);
      setOldPassword("");
      setNewPassword("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const field = (
    label: string,
    value: string,
    onChange: (v: string) => void,
    autoComplete: string,
  ) => (
    <div>
      <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">{label}</label>
      <input
        type="password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
      />
    </div>
  );

  return (
    <div className="css-glass space-y-4 !rounded-2xl p-5">
      {field("当前密码", oldPassword, setOldPassword, "current-password")}
      {field("新密码（至少 8 位）", newPassword, setNewPassword, "new-password")}
      {field("确认新密码", confirm, setConfirm, "new-password")}
      {error && <p className="text-sub text-[var(--danger)]">{error}</p>}
      {done && (
        <p className="text-sub text-[var(--text-muted)]">
          密码已修改，其他设备的登录已全部失效；当前会话保持有效。
        </p>
      )}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={submit}
          disabled={busy || !oldPassword || !newPassword || !confirm}
          className="btn-accent rounded-full px-4.5 py-2 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "提交中…" : "修改密码"}
        </button>
      </div>
    </div>
  );
}

/**
 * —— 应用分区：两类设置，胶囊标签切换（与外观分区同一交互语言） ——
 *
 *   - 版本与更新：当前版本、检查/执行更新、NER 模型、回退（AppUpdateSection）；
 *   - 网络与维护：外部访问地址、重启应用（AppConfigSection）；
 *   - 远程转码：远程 Worker 的开关与在线状态（RemoteTranscodeSection）。
 *     令牌不在这里——Worker 的凭证是逐台配对签发的，审批与吊销在「设备」分区。
 *
 * 为什么「版本与更新」是默认标签：这一页的高频入口是侧栏的更新徽标（有新版
 * 才出现），用户带着"来更新"的意图落地，第一屏就该是更新卡片；外部访问地址
 * 与重启是装完起初配一次/出问题才碰的低频项，收进第二个标签，不再和更新流程
 * 挤在一条长页里。有可用更新时标签上点一颗小蓝点，与设置侧栏的「应用」行同款。
 */
function AppSection() {
  const router = useRouter();
  const [tab, setTab] = useState<"update" | "maintain" | "remote">("update");
  // 支持 /settings/app?tab=remote 深链接：软转同意弹窗要把管理员直接送到
  // 「远程转码」，落在默认的「版本与更新」等于让引导断在最后一步。
  // 用 useEffect 而不是初始值读取，避免服务端渲染与客户端首帧不一致。
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested === "remote" || requested === "maintain" || requested === "update") {
      setTab(requested);
    }
  }, []);
  // 本分区只对管理员渲染（成员的分区清单里没有 app），无需再按角色关轮询
  const pendingUpdate = usePendingUpdate();
  const tabs = [
    { id: "update" as const, label: "版本与更新" },
    { id: "maintain" as const, label: "网络与维护" },
    { id: "remote" as const, label: "远程转码" },
  ] as const;

  return (
    <div className="space-y-5">
      <div className="flex gap-1.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={t.id === tab}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
              t.id === tab
                ? "bg-white/[0.14] text-white"
                : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
            }`}
          >
            {t.label}
            {t.id === "update" && pendingUpdate && <AppUpdateDot />}
          </button>
        ))}
      </div>
      {tab === "update" ? (
        <AppUpdateSection />
      ) : tab === "maintain" ? (
        <AppConfigSection />
      ) : (
        <RemoteTranscodeSection
          onOpenDevices={() => router.push("/settings/devices" as Route)}
        />
      )}
    </div>
  );
}

/**
 * —— 外观分区：两类设置，胶囊标签切换 ——
 *
 *   - 背景图：首页背景图库的管理（见 BackdropGroup）；
 *   - 界面质感：侧栏玻璃 + 背景蒙版的滑杆（见 InterfaceTextureGroup）。
 *
 * 全站蒙版默认就是「浅暗 + 轻模糊」的轻档（背景大图隐约透出），因此两类
 * 设置在本分区内即所见即所得：换图立刻全屏生效，调玻璃/蒙版滑杆也对着
 * 真实背景实时预览。效果本身就是提示，不再放文字说明条。
 * 标签切换与详情页「剧照/海报」同一交互语言；切走界面质感时未保存的滑杆
 * 草稿自动撤销（组件卸载即触发既有的清理逻辑）。
 */
function AppearanceSection() {
  const [tab, setTab] = useState<"backdrop" | "texture" | "nav">("backdrop");
  const tabs = [
    { id: "backdrop" as const, label: "背景图" },
    { id: "texture" as const, label: "界面质感" },
    { id: "nav" as const, label: "导航顺序" },
  ] as const;

  return (
    <div className="space-y-5">
      <div className="flex gap-1.5">
        {tabs.map((t) => (
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
      {tab === "backdrop" ? (
        <BackdropGroup />
      ) : tab === "texture" ? (
        <InterfaceTextureGroup />
      ) : (
        <NavOrderGroup />
      )}
    </div>
  );
}

/**
 * —— 背景图：首页背景图库 ——
 *
 * 交互对齐 macOS 壁纸 / iOS 锁屏编辑的三个手法：
 *   1) 实时预览：全站蒙版默认轻档、背景大图隐约透出，换图立刻全屏生效；
 *   2) 画廊瓷砖：默认图 + 图库里的每张自定义图排成可横滑的缩略图行，点选即切换，
 *      当前项带高亮环 + 对勾；hover 自定义瓷砖浮出 × 可单独删除；上传是「+」瓷砖；
 *   3) 大预览即投放区：hover 浮出更换按钮，拖图片进来直接换，点击任意处打开文件选择。
 *
 * 上传的图**全部保留**在当前账号的服务端图库，成员之间互不可见；每个账号
 * 最多 20 张，可随时点选切换。切回默认不删图，只有点瓷砖上的 × 才真正删除。
 * 生效图同时驱动 body::before 铺底与所有液态玻璃面板的折射纹理（见 lib/backdrop.tsx）。
 */
function BackdropGroup() {
  const { backdrop, isCustom, loading, items, activeId, uploadBackdrop, selectBackdrop, deleteBackdrop } =
    useBackdrop();
  const confirm = useConfirm();
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  /** 统一的写操作包装：清错误 + busy 防抖 + 中文错误回显。 */
  const guard = async (fn: () => Promise<void>, fallback: string) => {
    setError(null);
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  const handleFile = (file: File) => guard(() => uploadBackdrop(file), "上传失败，请重试");

  const handlePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // 清空 value：否则再次选择同一张图不会触发 change 事件。
    e.target.value = "";
    if (file) void handleFile(file);
  };

  /** 点选切换生效图（含切回默认）：不删任何图，无需确认。 */
  const handleSelect = (backdropId: string | null) =>
    guard(() => selectBackdrop(backdropId), "切换失败，请重试");

  /** 删除图库中的一张图：不可恢复，需二次确认；删的是生效图时后端自动回退默认。 */
  const handleDelete = async (backdropId: string) => {
    const ok = await confirm({
      title: "删除这张背景图？",
      description: "删除后不可恢复。",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    void guard(() => deleteBackdrop(backdropId), "删除失败，请重试");
  };

  return (
    <SettingsGroup label="首页背景">
        <div className="space-y-4">
          {/* 大预览 = 投放区：点击换图 / 拖入换图 / hover 浮出操作 */}
          <div
            role="button"
            tabIndex={0}
            aria-label="点击或拖入图片更换首页背景"
            onClick={() => !busy && !loading && inputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                inputRef.current?.click();
              }
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={(e) => {
              // 只在真正离开容器时收起投放态（进入子元素也会触发 dragleave）
              if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false);
            }}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const file = e.dataTransfer.files?.[0];
              if (file && !busy) void handleFile(file);
            }}
            className="group relative aspect-[16/9] cursor-pointer overflow-hidden rounded-2xl border border-white/[0.14] bg-black/30 shadow-[0_28px_70px_-18px_rgba(0,0,0,0.7)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
          >
            <img
              src={backdrop}
              alt="当前首页背景预览"
              loading="lazy"
              decoding="async"
              className="h-full w-full object-cover transition-transform duration-500 ease-out group-hover:scale-[1.03]"
            />
            {/* 底部信息渐变 + 当前背景名 */}
            <div className="pointer-events-none absolute inset-x-0 bottom-0 h-28 bg-gradient-to-t from-black/65 to-transparent" />
            <div className="pointer-events-none absolute bottom-4 left-5 text-white">
              <p className="text-on-image text-body font-semibold">
                {isCustom ? "自定义背景" : "默认背景 · 深色调"}
              </p>
              <p className="text-on-image mt-0.5 text-caption text-white/75">
                点击或拖入图片即可更换
              </p>
            </div>
            {/* hover：轻压暗 + 中央浮出更换按钮 */}
            <div className="touch-reveal pointer-events-none absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition duration-300 group-hover:bg-black/25 group-hover:opacity-100">
              <span className="btn-accent rounded-full px-4.5 py-2 text-sub font-semibold">
                更换图片
              </span>
            </div>
            {/* 拖拽投放态 */}
            {dragging && (
              <div className="absolute inset-2 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-[var(--accent)] bg-black/50 backdrop-blur-sm">
                <p className="text-body font-semibold text-[var(--accent-strong)]">
                  松开，设为首页背景
                </p>
              </div>
            )}
            {/* 上传 / 加载中的遮罩 */}
            {(busy || loading) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/50 backdrop-blur-sm">
                <p className="text-body font-medium text-white/90">
                  {busy ? "正在应用…" : "加载中…"}
                </p>
              </div>
            )}
          </div>

          {/* 画廊瓷砖：默认常驻 + 图库全部图 + 上传，可横滑；点选即切换，hover 浮出 × 删除 */}
          {/* 负外边距 + 等量内边距：给选中环（ring-2，画在瓷砖外侧）留出溢出空间，
              否则会被 overflow-x-auto 容器的上/左边缘裁掉 */}
          <div className="scroll-none -mx-1.5 -mt-1.5 flex items-start gap-3.5 overflow-x-auto px-1.5 pt-1.5 pb-1">
            <BackdropTile
              src={BACKDROP}
              label="默认"
              active={!isCustom}
              disabled={busy || loading}
              onSelect={() => void handleSelect(null)}
            />
            {items.map((item, i) => (
              <BackdropTile
                key={item.id}
                src={item.url}
                label={`自定义 ${i + 1}`}
                active={item.id === activeId}
                disabled={busy}
                onSelect={() => void handleSelect(item.id)}
                onDelete={() => void handleDelete(item.id)}
              />
            ))}
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={busy || loading}
              className="group/tile shrink-0 disabled:opacity-50"
            >
              <span className="flex h-[68px] w-[120px] items-center justify-center rounded-lg border border-dashed border-white/[0.22] bg-black/25 backdrop-blur-md transition-colors group-hover/tile:border-white/[0.4] group-hover/tile:bg-white/[0.06]">
                <PlusIcon className="size-5 text-[var(--text-muted)] transition-colors group-hover/tile:text-[var(--text)]" />
              </span>
              <span className="text-on-image mt-1.5 block text-center text-caption text-[var(--text-muted)]">
                上传
              </span>
            </button>
          </div>

          <input ref={inputRef} type="file" accept="image/*" hidden onChange={handlePick} />

          {error && (
            <p className="rounded-xl border border-[var(--danger)]/30 bg-[var(--danger)]/10 px-4 py-2.5 text-sub text-[var(--danger)]">
              {error}
            </p>
          )}

          <p className="text-on-image text-sub leading-5 text-[var(--text-faint)]">
            建议使用 16:9、分辨率较高的横图。上传的图全部保留在服务端图库（最多 20
            张），点选即切换、hover 缩略图可删除；玻璃面板的折射随生效图一并更新，跨设备访问同一实例保持一致。
          </p>
        </div>
    </SettingsGroup>
  );
}

/**
 * —— 界面质感：侧栏玻璃 + 背景蒙版的滑杆 ——
 *
 * 两组设定都存 ui.preferences 配置域：
 *   - 侧栏玻璃（sidebar 分组）：透明度 / 明暗 / 厚度三根滑杆，参数与 WebGL
 *     着色器的映射见 lib/glass.ts 的 sidebarGlass()；预览对象是左侧设置侧栏
 *     （与工作台侧栏同源），拖动即实时变化。
 *   - 背景蒙版（scrim 分组）：模糊度 / 暗度两根滑杆，驱动全站统一蒙版的
 *     backdrop-filter 与底色（--scrim-blur / --scrim-dark，见 lib/ui-prefs.tsx
 *     与 globals.css 的 .page-scrim）。本分区自己也铺着这层蒙版，拖动滑杆
 *     当场可见。
 *
 * 交互：拖动滑杆 → 写入 UiPrefs 预览草稿 → 侧栏 / 蒙版即时跟随（未落库）；
 * 「保存」才落库，离开本分区时未保存的草稿自动撤销；「恢复默认」一键回默认并保存。
 */
function InterfaceTextureGroup() {
  const { savedPrefs, savePrefs, setPreview, loading } = useUiPrefs();
  // 草稿覆盖本组管理的两个分组（侧栏玻璃 + 蒙版），其余分组保持 savedPrefs 原样
  const saved = { sidebar: savedPrefs.sidebar, scrim: savedPrefs.scrim };
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 已保存值变化（首次拉取完成 / 保存成功）时，把草稿对齐到最新落库值
  useEffect(() => setDraft(saved), [saved.sidebar, saved.scrim]); // eslint-disable-line react-hooks/exhaustive-deps

  // 离开外观分区时撤销未保存的预览，侧栏 / 蒙版回到已保存的样式
  useEffect(() => () => setPreview(null), [setPreview]);

  /** 拖动滑杆：更新草稿并作为预览即时应用（未落库） */
  const update = (patch: { sidebar?: Partial<typeof saved.sidebar>; scrim?: Partial<typeof saved.scrim> }) => {
    const next = {
      sidebar: { ...draft.sidebar, ...patch.sidebar },
      scrim: { ...draft.scrim, ...patch.scrim },
    };
    setDraft(next);
    setPreview({ ...savedPrefs, ...next });
  };

  const save = async (next: typeof saved) => {
    setBusy(true);
    setError(null);
    try {
      await savePrefs({ ...savedPrefs, ...next });
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  /** 恢复默认：滑杆回到内置默认值并直接保存（侧栏 / 蒙版即刻恢复默认观感） */
  const reset = () => {
    const defaults = { sidebar: DEFAULT_UI_PREFS.sidebar, scrim: DEFAULT_UI_PREFS.scrim };
    setDraft(defaults);
    setPreview({ ...savedPrefs, ...defaults });
    void save(defaults);
  };

  const same = (a: typeof saved, b: typeof saved) =>
    a.sidebar.transparency === b.sidebar.transparency &&
    a.sidebar.brightness === b.sidebar.brightness &&
    a.sidebar.depth === b.sidebar.depth &&
    a.scrim.blur === b.scrim.blur &&
    a.scrim.dark === b.scrim.dark;
  const dirty = !same(draft, saved);
  const isDefault = same(draft, { sidebar: DEFAULT_UI_PREFS.sidebar, scrim: DEFAULT_UI_PREFS.scrim });

  return (
    <SettingsGroup label="界面质感">
      <div className="css-glass space-y-5 !rounded-2xl p-5">
        <SliderRow
          label="侧栏透明度"
          hint="玻璃材质的整体浓度：0% 为标准玻璃卡片，100% 玻璃完全隐去、直接透出页面背景"
          minLabel="标准"
          maxLabel="全透"
          value={Math.round(draft.sidebar.transparency * 100)}
          disabled={loading || busy}
          onChange={(v) => update({ sidebar: { transparency: v / 100 } })}
        />
        <SliderRow
          label="侧栏明暗"
          hint="玻璃底色的亮度：向左更暗、向右更亮"
          minLabel="暗"
          maxLabel="亮"
          value={Math.round(((draft.sidebar.brightness + 1) / 2) * 100)}
          disabled={loading || busy}
          onChange={(v) => update({ sidebar: { brightness: (v / 100) * 2 - 1 } })}
        />
        <SliderRow
          label="侧栏厚度"
          hint="玻璃的边缘曲率带宽度：越大越像厚玻璃、边缘折射带越宽"
          minLabel="薄"
          maxLabel="厚"
          min={10}
          max={90}
          unit=""
          value={Math.round(draft.sidebar.depth)}
          disabled={loading || busy}
          onChange={(v) => update({ sidebar: { depth: v } })}
        />
        <SliderRow
          label="蒙版模糊度"
          hint="全站背景蒙版的模糊程度：0 背景清晰透出，越大背景越朦胧"
          minLabel="清晰"
          maxLabel="朦胧"
          min={0}
          max={40}
          unit=""
          value={Math.round(draft.scrim.blur)}
          disabled={loading || busy}
          onChange={(v) => update({ scrim: { blur: v } })}
        />
        <SliderRow
          label="蒙版暗度"
          hint="蒙版把背景压暗的程度：0% 完全不压暗，100% 全黑"
          minLabel="透亮"
          maxLabel="全黑"
          value={Math.round(draft.scrim.dark * 100)}
          disabled={loading || busy}
          onChange={(v) => update({ scrim: { dark: v / 100 } })}
        />

        {error && <p className="text-sub text-[var(--danger)]">{error}</p>}

        <div className="flex items-center justify-between gap-3">
          <p className="text-caption text-[var(--text-faint)]">
            {dirty ? "调节实时预览中，保存后对所有设备生效" : "设置已保存，跨设备一致"}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={reset}
              disabled={loading || busy || isDefault}
              className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
            >
              恢复默认
            </button>
            <button
              type="button"
              onClick={() => void save(draft)}
              disabled={loading || busy || !dirty}
              className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold disabled:opacity-40"
            >
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </SettingsGroup>
  );
}

/**
 * —— 导航顺序：侧栏主导航的个人排序 ——
 *
 * 存 `ui.preferences.nav.order`，超管与成员各存各的（`/ui/preferences` 按主体
 * 分流）。排的是**主导航**那一列，含「活动」——它虽然带角标，但对管理员是常驻
 * 一行，位置可以由人定。待处理与更新入口则不参与排序：这两个常态零渲染、有事
 * 才亮起，让用户给一个时有时无的行指定位置，排完的次序反而不可预期。
 *
 * 交互与「界面质感」同一套语言：改动即写入 UiPrefs 预览草稿 → 左侧真实侧栏
 * 当场重排（未落库），「保存」才落库，离开分区自动撤销草稿。
 *
 * 两种改序方式并存不是冗余：拖拽是桌面端的直觉操作，但 HTML5 拖放在触屏上
 * 根本不触发，键盘用户也用不了——上/下移按钮既是触屏与键盘的唯一入口，
 * 也是拖拽失手时的兜底。这也是本方案不做"侧栏内长按拖拽"的同一个理由：
 * 窄屏侧栏是抽屉，长按拖拽要和纵向滚动、抽屉滑动关闭抢同一串触摸事件。
 */
function NavOrderGroup() {
  const { savedPrefs, savePrefs, setPreview, loading } = useUiPrefs();
  const items = useVisibleNavItems();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 拖拽中的行下标；null = 没在拖
  const [dragIndex, setDragIndex] = useState<number | null>(null);

  // 依赖用拼接后的字符串而不是数组本身：items 每次渲染都是新数组（filter 的结果），
  // 直接进依赖会让下面的 useEffect 每帧都把草稿重置一遍
  const itemsKey = items.map((item) => item.id).join("|");
  const orderKey = savedPrefs.nav.order.join("|");
  const savedOrder = useMemo(
    () => applyNavOrder(items, savedPrefs.nav.order).map((item) => item.id),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [itemsKey, orderKey],
  );
  const defaultOrder = useMemo(
    () => items.map((item) => item.id),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [itemsKey],
  );
  const [draft, setDraft] = useState(savedOrder);

  // 已保存值变化（首次拉取完成 / 保存成功）时把草稿对齐到最新落库值
  useEffect(() => setDraft(savedOrder), [savedOrder]);
  // 离开外观分区时撤销未保存的预览，侧栏顺序回到已保存状态
  useEffect(() => () => setPreview(null), [setPreview]);

  const byId = new Map(items.map((item) => [item.id, item]));
  const rows = draft.map((id) => byId.get(id)).filter((item) => item != null);

  /** 顺序落到草稿并即时预览（未落库）。不可见项的 id 在这里一并保留。 */
  const apply = (nextOrder: string[]) => {
    setDraft(nextOrder);
    setPreview({
      ...savedPrefs,
      nav: { order: mergeNavOrder(nextOrder, savedPrefs.nav.order) },
    });
  };

  const move = (from: number, to: number) => {
    if (to < 0 || to >= draft.length || from === to) return;
    const next = [...draft];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    apply(next);
  };

  const save = async (nextOrder: string[]) => {
    setBusy(true);
    setError(null);
    try {
      await savePrefs({
        ...savedPrefs,
        nav: { order: mergeNavOrder(nextOrder, savedPrefs.nav.order) },
      });
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  /** 恢复默认：存空列表而不是存一份"恰好等于默认"的顺序——这样将来默认顺序
   *  调整、或新增导航入口时，没自定义过的人自动跟随新的默认排布。 */
  const reset = () => {
    setDraft(defaultOrder);
    setPreview({ ...savedPrefs, nav: { order: [] } });
    setBusy(true);
    setError(null);
    savePrefs({ ...savedPrefs, nav: { order: [] } })
      .catch((err) => setError(err instanceof HttpError ? err.message : "保存失败，请稍后重试"))
      .finally(() => setBusy(false));
  };

  const dirty = !sameNavOrder(draft, savedOrder);
  const isDefault = sameNavOrder(draft, defaultOrder);
  const disabled = loading || busy;

  return (
    <SettingsGroup label="导航顺序">
      <div className="css-glass space-y-4 !rounded-2xl p-5">
        <p className="text-sub leading-6 text-[var(--text-muted)]">
          调整左侧栏主导航的排列次序：拖动条目，或用右侧的上下按钮。待处理与更新入口有事才
          出现，位置固定，不参与排序。
        </p>

        <ul className="space-y-1.5">
          {rows.map((item, index) => {
            const Icon = item.icon;
            return (
              <li
                key={item.id}
                draggable={!disabled}
                onDragStart={() => setDragIndex(index)}
                onDragEnd={() => setDragIndex(null)}
                // 拖到哪就换到哪（跟手实时重排），而不是松手才生效：
                // 侧栏预览同步跟着变，所见即所得
                onDragEnter={() => {
                  if (dragIndex == null || dragIndex === index) return;
                  move(dragIndex, index);
                  setDragIndex(index);
                }}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => e.preventDefault()}
                className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 transition ${
                  dragIndex === index
                    ? "border-white/20 bg-white/[0.1] opacity-60"
                    : "border-white/[0.08] bg-white/[0.035]"
                } ${disabled ? "opacity-50" : "cursor-grab active:cursor-grabbing"}`}
              >
                <GripIcon className="size-4 shrink-0 text-[var(--text-faint)]" />
                <Icon className="size-[18px] shrink-0 text-[var(--text-muted)]" />
                <span className="flex-1 truncate text-ui font-medium text-[var(--text)]">
                  {item.label}
                </span>
                <span className="tnum shrink-0 text-caption text-[var(--text-faint)]">
                  {index + 1}
                </span>
                <div className="flex shrink-0 items-center gap-1">
                  <MoveButton
                    label={`把「${item.label}」上移`}
                    disabled={disabled || index === 0}
                    up
                    onClick={() => move(index, index - 1)}
                  />
                  <MoveButton
                    label={`把「${item.label}」下移`}
                    disabled={disabled || index === rows.length - 1}
                    onClick={() => move(index, index + 1)}
                  />
                </div>
              </li>
            );
          })}
        </ul>

        {error && <p className="text-sub text-[var(--danger)]">{error}</p>}

        <div className="flex items-center justify-between gap-3">
          <p className="text-caption text-[var(--text-faint)]">
            {dirty ? "左侧栏实时预览中，保存后对所有设备生效" : "设置已保存，跨设备一致"}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={reset}
              disabled={disabled || isDefault}
              className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
            >
              恢复默认
            </button>
            <button
              type="button"
              onClick={() => void save(draft)}
              disabled={disabled || !dirty}
              className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold disabled:opacity-40"
            >
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </SettingsGroup>
  );
}

/** 上/下移一格。触屏与键盘用户改序**只能**靠它——HTML5 拖放在触屏上不触发。 */
function MoveButton({
  label,
  up = false,
  disabled,
  onClick,
}: {
  label: string;
  up?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      // 窄屏放大到 44px（iOS HIG 最小可点目标）：触屏上 HTML5 拖放根本不触发，
      // 这两颗键是触屏用户改序的唯一入口，不能按普通图标键的密度做
      className="glass-row !size-8 justify-center !p-0 disabled:opacity-25 max-md:!size-11"
    >
      <ChevronDownIcon className={`size-4 ${up ? "rotate-180" : ""}`} />
    </button>
  );
}

/** 一行滑杆：标题 + 当前值 + 两端刻度提示。默认量程 0~100、以 % 展示；
 *  非百分比量（如厚度的 px 值）传 min/max 自定量程、unit 传空串展示裸数值。 */
function SliderRow({
  label,
  hint,
  minLabel,
  maxLabel,
  min = 0,
  max = 100,
  unit = "%",
  value,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  minLabel: string;
  maxLabel: string;
  min?: number;
  max?: number;
  unit?: string;
  value: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <p className="text-body font-medium text-[var(--text)]">{label}</p>
        <span className="tnum text-sub text-[var(--text-muted)]">
          {value}
          {unit}
        </span>
      </div>
      <p className="mt-0.5 text-caption text-[var(--text-faint)]">{hint}</p>
      <div className="mt-2.5 flex items-center gap-3">
        <span className="w-10 shrink-0 text-right text-caption text-[var(--text-faint)]">
          {minLabel}
        </span>
        <input
          type="range"
          min={min}
          max={max}
          step={1}
          value={value}
          disabled={disabled}
          aria-label={label}
          onChange={(e) => onChange(Number(e.target.value))}
          className="range-glass flex-1"
        />
        <span className="w-10 shrink-0 text-caption text-[var(--text-faint)]">{maxLabel}</span>
      </div>
    </div>
  );
}

/**
 * 背景画廊瓷砖：小尺寸缩略图 + 底部标签。
 * 当前生效项带银色高亮环 + 右上角对勾徽标（对齐 macOS 壁纸画廊的选中态），
 * 点击非选中项即切换；可删除的瓷砖（图库自定义图）hover 时左上角浮出 × 按钮。
 *
 * 结构上瓷砖本体是 div[role=button] 而非 <button>：删除 × 是内嵌的真按钮，
 * HTML 不允许 button 嵌套 button（会导致水合告警与不可预期的点击行为）。
 */
function BackdropTile({
  src,
  label,
  active,
  disabled = false,
  onSelect,
  onDelete,
}: {
  src: string;
  label: string;
  active: boolean;
  disabled?: boolean;
  onSelect?: () => void;
  onDelete?: () => void;
}) {
  const selectable = !active && !disabled;
  return (
    <div className={`group/tile w-[120px] shrink-0 ${disabled && !active ? "opacity-50" : ""}`}>
      <div
        role="button"
        tabIndex={selectable ? 0 : -1}
        aria-label={`使用${label}背景`}
        aria-disabled={!selectable}
        onClick={selectable ? onSelect : undefined}
        onKeyDown={(e) => {
          if (selectable && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onSelect?.();
          }
        }}
        className={`relative block h-[68px] w-[120px] overflow-hidden rounded-lg border outline-none transition focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] ${
          active
            ? "cursor-default border-transparent ring-2 ring-[var(--accent)] shadow-[0_6px_20px_-6px_rgba(180,198,230,0.35)]"
            : "cursor-pointer border-white/[0.14] group-hover/tile:border-white/[0.35]"
        }`}
      >
        <img
          src={src}
          alt={`${label}背景缩略图`}
          loading="lazy"
          decoding="async"
          className="h-full w-full object-cover"
        />
        {active && (
          <span className="absolute right-1.5 top-1.5 flex size-[18px] items-center justify-center rounded-full bg-[var(--accent-strong)] text-[#141821] shadow">
            <CheckIcon className="size-3" />
          </span>
        )}
        {onDelete && !disabled && (
          <button
            type="button"
            aria-label={`删除${label}背景图`}
            onClick={(e) => {
              // 阻止冒泡到瓷砖本体的「点选切换」
              e.stopPropagation();
              onDelete();
            }}
            className="absolute left-1.5 top-1.5 hidden size-[18px] items-center justify-center rounded-full bg-black/60 text-white/90 backdrop-blur transition-colors hover:bg-[var(--danger-solid)] hover:text-white group-hover/tile:flex"
          >
            <XIcon className="size-3" />
          </button>
        )}
      </div>
      <span
        className={`text-on-image mt-1.5 block truncate text-center text-caption ${
          active ? "font-semibold text-[var(--text)]" : "text-[var(--text-muted)]"
        }`}
      >
        {label}
      </span>
    </div>
  );
}

/* —— 其它分区：通用骨架（含真实液态玻璃开关，展示 WebGL 控件） —— */
function GenericSection({ sectionId }: { sectionId: string }) {
  const { backdrop } = useBackdrop();
  const toggles =
    sectionId === "notifications"
      ? ["任务完成时通知我", "任务失败时通知我", "每周巡检摘要"]
      : ["启用此模块", "记录审计日志"];

  return (
    <div className="space-y-5">
      <SettingsGroup label="偏好">
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {toggles.map((label, i) => (
            <div
              key={label}
              className="flex items-center justify-between gap-4 px-5 py-3.5"
            >
              <span className="text-body font-medium text-[var(--text)]">{label}</span>
              {/* LiquidGlassButton 本质是一个真实 WebGL 的开关控件 */}
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                defaultChecked={i === 0}
                aria-label={label}
                className="!min-h-0 !w-auto !bg-transparent !p-0"
              >
                <span className="sr-only">{label}</span>
              </LiquidGlassButton>
            </div>
          ))}
        </div>
      </SettingsGroup>

      <p className="text-sub leading-6 text-[var(--text-faint)]">
        这是「{sectionId}」分区的占位内容。后续会接入真实配置项与后端接口。
      </p>
    </div>
  );
}
