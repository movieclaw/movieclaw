"use client";

/**
 * 设置 -> 成员：成员账号、能力与资源范围的唯一管理入口。
 *
 * 列表只负责扫描状态，创建和编辑使用独立弹窗，避免在表格中展开长表单导致
 * 行高跳变。创建/重置产生的明文密码进入一次性结果弹窗，关闭后前端也不再
 * 保留，和后端“仅返回一次”的凭据语义一致。
 */

import { useCallback, useEffect, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { AvatarBadge } from "@/components/avatar-badge";
import { copyText } from "@/components/copy-button";
import { useConfirm, useToast } from "@/components/feedback";
import { CheckIcon, MoreIcon, PlusIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import {
  createMember,
  deleteMember,
  listMembers,
  resetMemberPassword,
  setMemberStatus,
  updateMember,
  type MemberUpdatePayload,
  type MemberView,
} from "@/lib/api/members";
import { listSiteCatalog, type CatalogItem } from "@/lib/api/sites";
import { formatRelativeTime } from "@/lib/time";

interface PasswordResult {
  title: string;
  username: string;
  password: string;
}

/** 生成随机初始密码（前端生成，创建前即可复制；后端只保存哈希）。 */
function generatePassword(): string {
  const alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ2345679";
  const bytes = crypto.getRandomValues(new Uint8Array(14));
  return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join("");
}

export function MembersSection() {
  const toast = useToast();
  const confirm = useConfirm();
  const [members, setMembers] = useState<MemberView[] | null>(null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [sites, setSites] = useState<CatalogItem[]>([]);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<MemberView | null>(null);
  const [passwordResult, setPasswordResult] = useState<PasswordResult | null>(null);

  const reload = useCallback(async () => setMembers(await listMembers()), []);

  useEffect(() => {
    void reload().catch((error) => toast.error(`加载成员失败：${(error as Error).message}`));
    void listLibraries().then(setLibraries).catch(() => {});
    void listSiteCatalog().then(setSites).catch(() => {});
  }, [reload, toast]);

  const replaceMember = (next: MemberView) => {
    setMembers((rows) => rows?.map((row) => (row.id === next.id ? next : row)) ?? rows);
    setEditing((current) => (current?.id === next.id ? next : current));
  };

  const resetPassword = async (member: MemberView) => {
    const accepted = await confirm({
      title: `重置「${member.nickname}」的密码？`,
      description: "旧密码和该成员的全部登录会立即失效。新密码只显示一次。",
      confirmLabel: "重置密码",
    });
    if (!accepted) return;
    try {
      const result = await resetMemberPassword(member.id);
      setPasswordResult({
        title: "密码已重置",
        username: result.username,
        password: result.password,
      });
    } catch (error) {
      toast.error(`重置失败：${(error as Error).message}`);
    }
  };

  const toggleStatus = async (member: MemberView) => {
    const enabling = member.status !== "active";
    if (!enabling) {
      const accepted = await confirm({
        title: `停用「${member.nickname}」？`,
        description: "该成员的全部设备会立即下线，个人数据和订阅保留，可随时重新启用。",
        confirmLabel: "停用成员",
        tone: "danger",
      });
      if (!accepted) return;
    }
    try {
      replaceMember(await setMemberStatus(member.id, enabling));
      toast.success(enabling ? "成员已启用" : "成员已停用");
    } catch (error) {
      toast.error(`操作失败：${(error as Error).message}`);
    }
  };

  const removeMember = async (member: MemberView) => {
    const accepted = await confirm({
      title: `删除成员「${member.nickname}」？`,
      description:
        "头像、播放进度等个人数据会被清理；订阅转由管理员接管，已下载内容不受影响。此操作不可恢复。",
      confirmLabel: "删除成员",
      tone: "danger",
    });
    if (!accepted) return;
    try {
      await deleteMember(member.id);
      setMembers((rows) => rows?.filter((row) => row.id !== member.id) ?? rows);
      setEditing(null);
      toast.success("成员已删除");
    } catch (error) {
      toast.error(`删除失败：${(error as Error).message}`);
    }
  };

  return (
    <section>
      <div className="mb-3 flex items-center justify-between gap-4">
        <div>
          <h3 className="text-body font-semibold text-[var(--text)]">成员账号</h3>
          <p className="mt-0.5 text-caption text-[var(--text-faint)]">
            管理登录状态、功能权限和可见媒体库
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="btn-accent flex shrink-0 items-center gap-1.5 rounded-full px-4 py-2 text-sub font-semibold"
        >
          <PlusIcon className="size-4" />
          添加成员
        </button>
      </div>

      <div className="css-glass overflow-hidden !rounded-xl">
        <div className="grid grid-cols-[minmax(170px,1.25fr)_minmax(160px,1fr)_minmax(135px,.8fr)_130px_36px] gap-4 border-b border-white/[0.07] px-4 py-2.5 text-caption font-medium text-[var(--text-faint)] max-md:hidden">
          <span>成员</span>
          <span>功能权限</span>
          <span>媒体库范围</span>
          <span>最近活动</span>
          <span className="sr-only">操作</span>
        </div>
        {members === null ? (
          <div className="flex items-center justify-center gap-2 px-5 py-10 text-ui text-[var(--text-muted)]">
            <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
            正在加载成员…
          </div>
        ) : members.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-body font-medium text-[var(--text)]">还没有成员账号</p>
            <p className="mt-1 text-sub text-[var(--text-muted)]">
              添加后即可分别控制订阅、搜索和媒体库可见范围。
            </p>
          </div>
        ) : (
          <div className="divide-y divide-white/[0.055]">
            {members.map((member) => (
              <MemberTableRow
                key={member.id}
                member={member}
                libraries={libraries}
                onEdit={() => setEditing(member)}
                onResetPassword={() => void resetPassword(member)}
                onToggleStatus={() => void toggleStatus(member)}
                onDelete={() => void removeMember(member)}
              />
            ))}
          </div>
        )}
      </div>

      <CreateMemberDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(member, password) => {
          setCreating(false);
          setMembers((rows) => (rows ? [...rows, member] : [member]));
          setPasswordResult({ title: "成员已创建", username: member.username, password });
        }}
      />

      {editing && (
        <EditMemberDialog
          key={editing.id}
          member={editing}
          libraries={libraries}
          sites={sites}
          onClose={() => setEditing(null)}
          onSaved={(next) => {
            replaceMember(next);
            setEditing(null);
            toast.success("成员设置已保存");
          }}
        />
      )}

      <PasswordResultDialog result={passwordResult} onClose={() => setPasswordResult(null)} />
    </section>
  );
}

function MemberTableRow({
  member,
  libraries,
  onEdit,
  onResetPassword,
  onToggleStatus,
  onDelete,
}: {
  member: MemberView;
  libraries: MediaLibrary[];
  onEdit: () => void;
  onResetPassword: () => void;
  onToggleStatus: () => void;
  onDelete: () => void;
}) {
  const permissionLabels = [
    member.allow_subscribe ? "订阅" : null,
    member.allow_search ? "搜索" : null,
    member.allow_direct_download ? "下载" : null,
  ].filter(Boolean);
  const visibleNames = libraries
    .filter((library) => member.library_ids.includes(library.id))
    .map((library) => library.name);
  const libraryScope = member.all_libraries
    ? "全部媒体库"
    : visibleNames.length > 0
      ? visibleNames.join("、")
      : "未分配媒体库";

  return (
    <div className="grid grid-cols-[minmax(170px,1.25fr)_minmax(160px,1fr)_minmax(135px,.8fr)_130px_36px] items-center gap-4 px-4 py-3.5 transition-colors hover:bg-white/[0.025] max-md:grid-cols-[minmax(0,1fr)_36px] max-md:gap-x-3 max-md:gap-y-2.5">
      <div className="flex min-w-0 items-center gap-3">
        <AvatarBadge nickname={member.nickname} avatarUrl={member.avatar_url} className="size-9" />
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-ui font-semibold text-[var(--text)]">
              {member.nickname}
            </span>
            <span
              className={`size-1.5 shrink-0 rounded-full ${member.status === "active" ? "bg-[var(--ok)]" : "bg-white/25"}`}
              title={member.status === "active" ? "已启用" : "已停用"}
            />
          </div>
          <p className="truncate text-caption text-[var(--text-faint)]">@{member.username}</p>
        </div>
      </div>
      <div className="flex min-w-0 flex-wrap gap-1.5 max-md:col-start-1">
        {permissionLabels.length > 0 ? (
          permissionLabels.map((label) => <Badge key={label}>{label}</Badge>)
        ) : (
          <span className="text-caption text-[var(--text-faint)]">仅浏览与播放</span>
        )}
      </div>
      <p className="truncate text-sub text-[var(--text-muted)] max-md:col-start-1" title={libraryScope}>
        {libraryScope}
      </p>
      <p className="text-caption text-[var(--text-faint)] max-md:col-start-1">
        {member.last_login_at ? formatRelativeTime(member.last_login_at) : "从未登录"}
      </p>
      <MemberActionsMenu
        member={member}
        onEdit={onEdit}
        onResetPassword={onResetPassword}
        onToggleStatus={onToggleStatus}
        onDelete={onDelete}
      />
    </div>
  );
}

function MemberActionsMenu({
  member,
  onEdit,
  onResetPassword,
  onToggleStatus,
  onDelete,
}: {
  member: MemberView;
  onEdit: () => void;
  onResetPassword: () => void;
  onToggleStatus: () => void;
  onDelete: () => void;
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-sub font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)]";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`${member.nickname} 的更多操作`}
          title="更多操作"
          className="glass-row !size-8 justify-center !p-0 data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)] max-md:col-start-2 max-md:row-start-1"
        >
          <MoreIcon className="size-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[9rem] p-1"
        >
          <DropdownMenu.Item onSelect={onEdit} className={itemClass}>
            编辑成员
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onResetPassword} className={itemClass}>
            重置密码
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          <DropdownMenu.Item
            onSelect={onToggleStatus}
            className={`${itemClass} ${member.status === "active" ? "!text-[#ffb36b] data-[highlighted]:!bg-[#ffb36b]/10" : ""}`}
          >
            {member.status === "active" ? "停用成员" : "启用成员"}
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onDelete}
            className={`${itemClass} !text-[#ff6b6b] data-[highlighted]:!bg-[#ff6b6b]/10`}
          >
            删除成员
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function CreateMemberDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (member: MemberView, password: string) => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [nickname, setNickname] = useState("");
  const [password, setPassword] = useState(() => generatePassword());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setUsername("");
    setNickname("");
    setPassword(generatePassword());
    setBusy(false);
  }, [open]);

  const submit = async () => {
    if (username.trim().length < 3) return toast.error("用户名至少 3 个字符");
    if (password.length < 8) return toast.error("密码至少 8 位");
    setBusy(true);
    try {
      const member = await createMember(username.trim(), password, nickname.trim());
      onCreated(member, password);
    } catch (error) {
      toast.error(`创建失败：${(error as Error).message}`);
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} label="添加成员" width="lg">
      <div className="p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">添加成员</h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">
          新成员默认可以订阅和浏览全部媒体库，站点搜索默认关闭。
        </p>

        <div className="mt-5 grid grid-cols-2 gap-4 max-md:grid-cols-1">
          <Field label="用户名" hint="登录使用，创建后不可修改">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoFocus
              autoComplete="off"
              placeholder="如 yee"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="昵称" hint="页面展示名，可留空">
            <input
              value={nickname}
              onChange={(event) => setNickname(event.target.value)}
              placeholder="如 小叶"
              className={INPUT_CLASS}
            />
          </Field>
        </div>
        <div className="mt-4">
          <SectionTitle
            title="初始密码"
            description="已随机生成。点击密码行即可复制，创建成功后还会显示一次。"
          />
          <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-black/20">
            <CredentialRow label="密码" value={password} mono />
            <button
              type="button"
              onClick={() => setPassword(generatePassword())}
              className="flex w-full items-center justify-between border-t border-white/[0.08] bg-white/[0.035] px-4 py-3 text-left text-sub font-medium text-white/85 transition-colors hover:bg-white/[0.07]"
            >
              重新生成密码
              <span className="text-caption text-[var(--text-faint)]">换一个随机密码</span>
            </button>
          </div>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
            取消
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void submit()}
            className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
          >
            {busy ? "创建中…" : "创建成员"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function EditMemberDialog({
  member,
  libraries,
  sites,
  onClose,
  onSaved,
}: {
  member: MemberView;
  libraries: MediaLibrary[];
  sites: CatalogItem[];
  onClose: () => void;
  onSaved: (member: MemberView) => void;
}) {
  const toast = useToast();
  const [nickname, setNickname] = useState(member.nickname);
  const [allowSubscribe, setAllowSubscribe] = useState(member.allow_subscribe);
  const [allowSearch, setAllowSearch] = useState(member.allow_search);
  const [allowDirectDownload, setAllowDirectDownload] = useState(member.allow_direct_download);
  const [allLibraries, setAllLibraries] = useState(member.all_libraries);
  const [libraryIds, setLibraryIds] = useState(member.library_ids);
  const [allSites, setAllSites] = useState(member.all_sites);
  const [siteIds, setSiteIds] = useState(member.site_ids);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    const payload: MemberUpdatePayload = {
      nickname: nickname.trim(),
      allow_subscribe: allowSubscribe,
      allow_search: allowSearch,
      allow_direct_download: allowSearch && allowDirectDownload,
      all_libraries: allLibraries,
      library_ids: allLibraries ? [] : libraryIds,
      all_sites: allSites,
      site_ids: allSites ? [] : siteIds,
    };
    try {
      onSaved(await updateMember(member.id, payload));
    } catch (error) {
      toast.error(`保存失败：${(error as Error).message}`);
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      label={`编辑成员 ${member.nickname}`}
      width="2xl"
      panelClassName="flex max-h-[82dvh] flex-col"
    >
      <div className="scroll-thin overflow-y-auto p-6 max-md:p-5">
        <div className="flex items-center gap-3">
          <AvatarBadge nickname={member.nickname} avatarUrl={member.avatar_url} className="size-11" />
          <div className="min-w-0">
            <h2 className="truncate text-title font-bold text-white">编辑成员</h2>
            <p className="truncate text-sub text-[var(--text-muted)]">@{member.username}</p>
          </div>
          <StatusBadge status={member.status} />
        </div>

        <div className="mt-6 border-t border-white/[0.07] pt-5">
          <SectionTitle title="基本信息" />
          <Field label="昵称">
            <input value={nickname} onChange={(event) => setNickname(event.target.value)} className={INPUT_CLASS} />
          </Field>
        </div>

        <div className="mt-6 border-t border-white/[0.07] pt-5">
          <SectionTitle title="功能权限" description="关闭后相关入口会从成员页面隐藏，后端同时拒绝调用。" />
          <div className="divide-y divide-white/[0.055]">
            <PermissionToggle
              label="订阅追踪"
              description="发起订阅并管理自己的订阅"
              checked={allowSubscribe}
              onChange={setAllowSubscribe}
            />
            <PermissionToggle
              label="站点搜索"
              description="搜索被分配的 PT 站点资源"
              checked={allowSearch}
              onChange={(checked) => {
                setAllowSearch(checked);
                if (!checked) setAllowDirectDownload(false);
              }}
            />
            <PermissionToggle
              label="一键下载"
              description="从搜索结果直接提交下载，依赖站点搜索"
              checked={allowDirectDownload}
              disabled={!allowSearch}
              onChange={setAllowDirectDownload}
            />
          </div>
        </div>

        <div className="mt-6 border-t border-white/[0.07] pt-5">
          <SectionTitle title="媒体库范围" />
          <AccessPicker
            allSelected={allLibraries}
            allLabel="全部媒体库"
            limitedLabel="指定媒体库"
            options={libraries.map((library) => ({ id: library.id, label: library.name }))}
            selected={libraryIds}
            onModeChange={setAllLibraries}
            onSelectedChange={setLibraryIds}
          />
        </div>

        {allowSearch && (
          <div className="mt-6 border-t border-white/[0.07] pt-5">
            <SectionTitle title="可搜索站点" />
            <AccessPicker
              allSelected={allSites}
              allLabel="全部启用站点"
              limitedLabel="指定站点"
              options={sites.map((site) => ({ id: site.site_id, label: site.display_name }))}
              selected={siteIds}
              onModeChange={setAllSites}
              onSelectedChange={setSiteIds}
            />
          </div>
        )}

      </div>

      <div className="flex shrink-0 justify-end gap-3 border-t border-white/[0.07] px-6 py-4 max-md:px-5">
        <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
          取消
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void save()}
          className="btn-accent flex h-9 items-center gap-1.5 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
        >
          <CheckIcon className="size-4" />
          {busy ? "保存中…" : "保存设置"}
        </button>
      </div>
    </Modal>
  );
}

function PasswordResultDialog({
  result,
  onClose,
}: {
  result: PasswordResult | null;
  onClose: () => void;
}) {
  if (!result) return null;
  const credentialText = `账号：${result.username}\n密码：${result.password}`;
  return (
    <Modal open onClose={onClose} label={result.title} width="md" raised>
      <div className="p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">{result.title}</h2>
        <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
          密码只在这里显示一次。点击账号或密码即可复制，关闭弹窗后无法再次查看。
        </p>
        <div className="mt-5 overflow-hidden rounded-xl border border-white/[0.08] bg-black/20">
          <CredentialRow label="账号" value={result.username} />
          <CredentialRow label="密码" value={result.password} mono />
          <CopySurface
            text={credentialText}
            successMessage="账号和密码已复制"
            idleLabel=""
            className="flex w-full items-center justify-between border-t border-white/[0.08] bg-white/[0.035] px-4 py-3 text-left text-sub font-medium text-white/85 transition-colors hover:bg-white/[0.07]"
          >
            复制全部登录信息
          </CopySurface>
        </div>
        <div className="mt-5 flex justify-end">
          <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
            完成
          </button>
        </div>
      </div>
    </Modal>
  );
}

const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="mt-4 block first:mt-0">
      <span className="mb-1 flex items-baseline gap-2 text-sub font-medium text-[var(--text)]">
        {label}
        {hint && <span className="font-normal text-[var(--text-faint)]">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

function SectionTitle({ title, description }: { title: string; description?: string }) {
  return (
    <div className="mb-3">
      <h3 className="text-ui font-semibold text-white/90">{title}</h3>
      {description && <p className="mt-0.5 text-caption text-[var(--text-faint)]">{description}</p>}
    </div>
  );
}

function PermissionToggle({
  label,
  description,
  checked,
  disabled = false,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className={`flex items-center justify-between gap-5 py-3 ${disabled ? "opacity-40" : "cursor-pointer"}`}>
      <span className="min-w-0">
        <span className="block text-ui font-medium text-[var(--text)]">{label}</span>
        <span className="mt-0.5 block text-caption text-[var(--text-faint)]">{description}</span>
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="size-4 shrink-0 accent-[var(--accent)]"
      />
    </label>
  );
}

function AccessPicker<T extends string | number>({
  allSelected,
  allLabel,
  limitedLabel,
  options,
  selected,
  onModeChange,
  onSelectedChange,
}: {
  allSelected: boolean;
  allLabel: string;
  limitedLabel: string;
  options: { id: T; label: string }[];
  selected: T[];
  onModeChange: (all: boolean) => void;
  onSelectedChange: (selected: T[]) => void;
}) {
  const toggle = (id: T) =>
    onSelectedChange(selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id]);

  return (
    <div>
      <div className="inline-flex rounded-lg border border-white/[0.08] bg-white/[0.035] p-1">
        <ModeButton active={allSelected} onClick={() => onModeChange(true)}>{allLabel}</ModeButton>
        <ModeButton active={!allSelected} onClick={() => onModeChange(false)}>{limitedLabel}</ModeButton>
      </div>
      {!allSelected && (
        <div className="mt-3 flex flex-wrap gap-2">
          {options.length === 0 ? (
            <p className="text-caption text-[var(--text-faint)]">暂无可选项</p>
          ) : (
            options.map((option) => (
              <button
                key={String(option.id)}
                type="button"
                onClick={() => toggle(option.id)}
                className={`rounded-lg border px-3 py-1.5 text-sub font-medium transition-colors ${
                  selected.includes(option.id)
                    ? "border-[var(--accent)]/50 bg-[var(--accent-soft)] text-[var(--accent)]"
                    : "border-white/[0.09] bg-white/[0.035] text-[var(--text-muted)] hover:text-[var(--text)]"
                }`}
              >
                {option.label}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function ModeButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sub font-medium transition-colors ${active ? "bg-white/[0.11] text-white" : "text-[var(--text-faint)] hover:text-[var(--text)]"}`}
    >
      {children}
    </button>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="rounded-md bg-white/[0.06] px-2 py-1 text-caption text-white/70">{children}</span>;
}

function StatusBadge({ status }: { status: MemberView["status"] }) {
  return (
    <span className={`ml-auto rounded-full border px-2.5 py-1 text-caption font-medium ${status === "active" ? "border-emerald-400/25 bg-[var(--ok)]/10 text-emerald-300" : "border-white/[0.1] bg-white/[0.04] text-[var(--text-faint)]"}`}>
      {status === "active" ? "已启用" : "已停用"}
    </span>
  );
}

function CredentialRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <CopySurface
      text={value}
      successMessage={`${label}已复制`}
      className="flex w-full items-center gap-4 border-b border-white/[0.06] px-4 py-3 text-left transition-colors hover:bg-white/[0.045]"
    >
      <span className="w-10 shrink-0 text-caption text-[var(--text-faint)]">{label}</span>
      <span className={`min-w-0 flex-1 break-all text-ui text-white ${mono ? "font-mono" : ""}`}>
        {value}
      </span>
    </CopySurface>
  );
}

function CopySurface({
  text,
  successMessage,
  idleLabel = "点击复制",
  className,
  children,
}: {
  text: string;
  successMessage: string;
  idleLabel?: string;
  className: string;
  children: React.ReactNode;
}) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      toast.success(successMessage);
    } catch {
      toast.error("复制失败，请长按内容手动复制");
    }
  };

  return (
    <button
      type="button"
      onClick={() => void copy()}
      aria-label={`${successMessage.replace("已复制", "")}，点击复制`}
      className={className}
    >
      {children}
      {(copied || idleLabel) && (
        <span
          aria-live="polite"
          className={`shrink-0 text-caption ${copied ? "text-emerald-300" : "text-[var(--text-faint)]"}`}
        >
          {copied ? "已复制" : idleLabel}
        </span>
      )}
    </button>
  );
}
