"use client";

import { useEffect, useState } from "react";

import { useConfirm, useToast } from "@/components/feedback";
import { CheckIcon, CopyIcon, LockIcon, RefreshIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import { copyText } from "@/components/copy-button";
import {
  type ShareView,
  createCollectionShare,
  createItemShare,
  revokeCollectionShare,
  revokeItemShare,
} from "@/lib/api/shares";
import { imageUrl } from "@/lib/image-proxy";
import { LIBRARY_KIND_LABELS, type LibraryKind } from "@/lib/media-types";
import {
  DEFAULT_SHARE_EXPIRY_DAYS,
  SHARE_EXPIRY_OPTIONS,
  absoluteShareUrl,
  expiryHint,
  generateSharePassword,
  isRelativeShareUrl,
  shareCopyText,
  validateSharePassword,
} from "@/lib/share";
import { formatDateTime, formatRelativeTime } from "@/lib/time";

/**
 * 影片分享对话框（docs/design/media-share.md §1.1）。两个形态一个组件：
 *
 *   (a) 还没分享 → 表单：有效期四档 + 密码开关（自动生成访问码，可改可换）；
 *   (b) 已有有效分享 → 链接 / 密码 / 到期 / 打开次数，复制与取消。
 *
 * 「生成链接」成功后原地切到 (b)，不关窗不二跳；「取消分享」二次确认后回到 (a)，
 * 可以马上再生成一条新链接（slug 换新）。
 */
export function ShareDialog({
  open,
  onClose,
  libraryId,
  mediaItemId,
  collectionId,
  title,
  kind,
  year,
  posterUrl,
  seasonSummary,
  initialShare,
}: {
  open: boolean;
  onClose: () => void;
  /** 分享一个条目时给；分享合集时不给 */
  libraryId?: number;
  mediaItemId?: number;
  /** 分享一个合集时给。两者恰好给一个——这条链接的范围就是二选一 */
  collectionId?: number;
  title: string;
  kind?: LibraryKind;
  year: number | null;
  posterUrl: string | null;
  /** 剧集的范围提醒（如「已入库 3 季 24 集」）；电影不传 */
  seasonSummary?: string | null;
  /** 打开时已知的有效分享（调用方先查过）；null = 还没分享 */
  initialShare: ShareView | null;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [share, setShare] = useState<ShareView | null>(initialShare);
  const [days, setDays] = useState(DEFAULT_SHARE_EXPIRY_DAYS);
  const [passwordOn, setPasswordOn] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 每次打开都从调用方给的现状开始；表单回到默认值
  useEffect(() => {
    if (!open) return;
    setShare(initialShare);
    setDays(DEFAULT_SHARE_EXPIRY_DAYS);
    setPasswordOn(false);
    setPassword("");
    setError(null);
  }, [open, initialShare]);

  // 合集分享没有"形态"与年份，副标题由调用方用 seasonSummary 那一格给
  // （「12 部 · 会自动收录新片」这类）
  const kindLabel = kind ? LIBRARY_KIND_LABELS[kind] : null;
  const subtitle = [
    year ? String(year) : null,
    seasonSummary ? [kindLabel, seasonSummary].filter(Boolean).join(" · ") : kindLabel,
  ]
    .filter(Boolean)
    .join(" · ");

  const togglePassword = () => {
    setError(null);
    if (passwordOn) {
      setPasswordOn(false);
      return;
    }
    setPasswordOn(true);
    if (!password.trim()) setPassword(generateSharePassword());
  };

  const create = async () => {
    const pw = passwordOn ? password : "";
    const invalid = passwordOn ? validateSharePassword(pw) : null;
    if (invalid) {
      setError(invalid);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // 范围二选一：合集分享与条目分享共用这个弹层，因为要填的东西
      // （有效期、密码）逐字相同，只有落到哪个接口不一样
      const body = {
        expires_in_days: days,
        password: passwordOn && pw.trim() ? pw.trim() : null,
      };
      const result =
        collectionId !== undefined
          ? await createCollectionShare(collectionId, body)
          : await createItemShare(libraryId ?? 0, mediaItemId ?? 0, body);
      setShare(result.share);
      toast.success(
        result.existed
          ? collectionId !== undefined
            ? "这个合集已有一条有效分享"
            : "这部影片已有一条有效分享"
          : "分享链接已生成",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "生成分享链接失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    const ok = await confirm({
      title: `取消《${title}》的分享？`,
      description: "链接立即失效，正在播放的访客会在一分钟内中断。之后可以重新生成一条新链接。",
      confirmLabel: "取消分享",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      if (collectionId !== undefined) await revokeCollectionShare(collectionId);
      else await revokeItemShare(libraryId ?? 0, mediaItemId ?? 0);
      setShare(null);
      toast.success("分享已取消");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "取消分享失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const copy = (text: string, what: string) => {
    void copyText(text)
      .then(() => toast.success(`已复制${what}`))
      .catch(() => toast.error("浏览器拒绝访问剪贴板，请手动复制"));
  };

  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const link = share ? absoluteShareUrl(share.url, origin) : "";

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} label="分享影片" width="lg">
      {/* 头部常驻：标题与影片身份——滚到表单底部时仍知道在分享哪一部 */}
      <div className="border-b border-white/[0.07] p-6">
        <h3 className="text-title-sm font-semibold text-[var(--text)]">
          {share ? `《${title}》已分享` : `分享《${title}》`}
        </h3>

        <div className="mt-4 flex items-center gap-3.5">
          <div className="relative h-[72px] w-12 shrink-0 overflow-hidden rounded-lg bg-white/[0.06]">
            <PosterImage src={imageUrl(posterUrl)} alt="" className="size-full object-cover" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-ui font-medium text-[var(--text)]">{title}</p>
            <p className="mt-0.5 text-caption text-[var(--text-muted)]">{subtitle}</p>
            <p className="mt-1 text-caption text-[var(--text-faint)]">
              任何拿到链接的人都能观看这部影片，不需要登录。
            </p>
          </div>
        </div>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-6 pb-6 pt-1">
        {share ? (
          <ShareReady
            share={share}
            link={link}
            busy={busy}
            onCopyLink={() => copy(link, "链接")}
            onCopyPassword={() => share.password && copy(share.password, "密码")}
            onCopyAll={() => copy(shareCopyText(title, link, share.password), "链接和密码")}
            onRevoke={revoke}
            onClose={onClose}
          />
        ) : (
          <>
            <div className="mt-5">
              <p className="text-sub font-medium text-[var(--text-muted)]">有效期</p>
              <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="有效期">
                {SHARE_EXPIRY_OPTIONS.map((option) => (
                  <button
                    key={option.days}
                    type="button"
                    role="radio"
                    aria-checked={days === option.days}
                    onClick={() => setDays(option.days)}
                    className={`rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
                      days === option.days
                        ? "bg-white/[0.16] text-white"
                        : "bg-white/[0.05] text-[var(--text-muted)] hover:bg-white/[0.09] hover:text-[var(--text)]"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <p className="mt-2 text-caption text-[var(--text-faint)]">
                {days} 天后自动失效（{formatDateTime(new Date(Date.now() + days * 86_400_000).toISOString())}）；到期后可以再分享一次。
              </p>
            </div>

            <div className="mt-5">
              <label className="flex cursor-pointer items-center justify-between gap-3">
                <span className="text-sub font-medium text-[var(--text-muted)]">密码保护</span>
                <input
                  type="checkbox"
                  checked={passwordOn}
                  onChange={togglePassword}
                  className="size-4 accent-[var(--accent)]"
                />
              </label>
              {passwordOn && (
                <div className="mt-2 flex items-center gap-2">
                  <input
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      setError(null);
                    }}
                    maxLength={32}
                    spellCheck={false}
                    autoComplete="off"
                    aria-label="访问密码"
                    className="tnum w-40 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setPassword(generateSharePassword());
                      setError(null);
                    }}
                    className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-sub text-[var(--text-muted)] transition-colors hover:bg-white/[0.07] hover:text-[var(--text)]"
                  >
                    <RefreshIcon className="size-3.5" />
                    换一个
                  </button>
                </div>
              )}
              <p className="mt-2 text-caption text-[var(--text-faint)]">
                {passwordOn
                  ? "访客打开链接时需要输入这个密码；密码之前不会显示片名和海报。"
                  : "不设密码：拿到链接就能看。"}
              </p>
            </div>

            {error && (
              <p className="mt-4 rounded-xl border border-[rgba(255,107,107,0.25)] bg-[rgba(255,107,107,0.1)] px-3 py-2 text-sub text-[var(--danger)]">
                {error}
              </p>
            )}

            <p className="mt-5 text-caption text-[var(--text-faint)]">
              访客的播放会出现在「活动」页，你随时可以取消分享。
            </p>
          </>
        )}
      </div>

      {/* 底栏常驻（仅创建态；已分享态的出口按钮在 ShareReady 里） */}
      {!share && (
        <div className="flex justify-end gap-2 border-t border-white/[0.07] px-6 py-4">
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            className="btn-glass px-4 py-2 text-ui text-[var(--text-muted)] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={create}
            className="rounded-xl bg-white px-4 py-2 text-ui font-semibold text-black transition hover:bg-white/90 disabled:opacity-50"
          >
            {busy ? "正在生成…" : "生成链接"}
          </button>
        </div>
      )}
    </Modal>
  );
}

function ShareReady({
  share,
  link,
  busy,
  onCopyLink,
  onCopyPassword,
  onCopyAll,
  onRevoke,
  onClose,
}: {
  share: ShareView;
  link: string;
  busy: boolean;
  onCopyLink: () => void;
  onCopyPassword: () => void;
  onCopyAll: () => void;
  onRevoke: () => void;
  onClose: () => void;
}) {
  const facts = [
    `${expiryHint(share.expires_at)}（${formatDateTime(share.expires_at)}）`,
    share.view_count > 0 ? `已打开 ${share.view_count} 次` : "还没有人打开",
    share.last_accessed_at ? `最近 ${formatRelativeTime(share.last_accessed_at)}` : null,
  ].filter(Boolean);

  return (
    <>
      <div className="mt-5 space-y-2">
        <ShareRow label="链接" value={link} onCopy={onCopyLink} />
        {share.password && (
          <ShareRow
            label="密码"
            value={share.password}
            mono
            icon={<LockIcon className="size-3.5 text-[var(--text-faint)]" />}
            onCopy={onCopyPassword}
          />
        )}
      </div>
      <p className="tnum mt-3 text-caption text-[var(--text-faint)]">{facts.join(" · ")}</p>
      {isRelativeShareUrl(share.url) && (
        <p className="mt-2 text-caption text-[var(--text-faint)]">
          链接用的是当前浏览器的地址。在「设置 → 网络 → 外部访问」填写外网地址后，链接会用该地址生成。
        </p>
      )}
      <div className="mt-5 flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          onClick={onCopyAll}
          className="inline-flex items-center gap-1.5 rounded-xl bg-white px-4 py-2 text-ui font-semibold text-black transition hover:bg-white/90"
        >
          <CopyIcon className="size-4" />
          {share.password ? "复制链接和密码" : "复制链接"}
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onRevoke}
            className="rounded-xl px-3.5 py-2 text-ui text-[#ff9f9f] transition-colors hover:bg-[rgba(255,90,90,0.16)] disabled:opacity-50"
          >
            取消分享
          </button>
          <button
            type="button"
            onClick={onClose}
            className="btn-glass px-4 py-2 text-ui text-[var(--text-muted)]"
          >
            关闭
          </button>
        </div>
      </div>
    </>
  );
}

function ShareRow({
  label,
  value,
  mono = false,
  icon,
  onCopy,
}: {
  label: string;
  value: string;
  mono?: boolean;
  icon?: React.ReactNode;
  onCopy: () => void;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <div className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5">
      <span className="w-8 shrink-0 text-sub text-[var(--text-muted)]">{label}</span>
      {icon}
      <span
        className={`min-w-0 flex-1 truncate text-ui text-[var(--text)] ${mono ? "font-mono" : ""}`}
        title={value}
      >
        {value}
      </span>
      <button
        type="button"
        onClick={() => {
          onCopy();
          setCopied(true);
        }}
        className="inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-1 text-sub text-[var(--text-muted)] transition-colors hover:bg-white/[0.07] hover:text-[var(--text)]"
      >
        {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
        {copied ? "已复制" : "复制"}
      </button>
    </div>
  );
}
