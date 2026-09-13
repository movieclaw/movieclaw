"use client";

/**
 * 取消订阅确认弹窗（管理员）。
 *
 * 取消订阅的本义只是"不再追了"，默认什么都不删——这是产品一直以来的承诺，
 * 两个开关都保持关闭。但"这部片我不要了"往往还想让它从机器上消失，此前要
 * 分头去下载器删任务、去媒体库删文件，用户根本找不到。于是把两件事收成这里
 * 的两个显式勾选，并在勾之前就把后果与数量讲清楚：
 *
 * - 种子：连同下载目录里的数据文件一起删，**不可恢复**；有 H&R 考核风险时
 *   额外给一行警示（PT 站点的考核不该由我们替用户静默牺牲）；
 * - 媒体库文件：进回收站，保留期内可在「媒体库 → 回收站」恢复。
 *
 * 数量由后端 removal-preview 给出；预览还没回来时两个开关先禁用——没数字的
 * 勾选框等于让用户盲签。
 */

import { useEffect, useState } from "react";

import { Modal } from "@/components/modal";
import { formatBytes } from "@/lib/format";
import {
  getSubscriptionRemovalPreview,
  type SubscriptionRemovalOptions,
  type SubscriptionRemovalPreview,
} from "@/lib/api/subscriptions";

const CANCEL_BTN_CLS =
  "rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1]";
const DANGER_BTN_CLS =
  "rounded-lg bg-red-500/85 px-4 py-2 text-ui font-medium text-white transition hover:bg-red-500 disabled:opacity-50";

export function SubscriptionCancelDialog({
  open,
  subscriptionId,
  title,
  raised = false,
  onClose,
  onConfirm,
}: {
  open: boolean;
  subscriptionId: number;
  /** 作品名（弹窗标题与文案里出现） */
  title: string;
  /** 叠在另一个弹窗之上时置 true（订阅管理弹层里的取消订阅） */
  raised?: boolean;
  onClose: () => void;
  /** 用户确认：带上两个清理开关的最终取值 */
  onConfirm: (options: SubscriptionRemovalOptions) => Promise<void> | void;
}) {
  const [preview, setPreview] = useState<SubscriptionRemovalPreview | null>(null);
  const [deleteTorrents, setDeleteTorrents] = useState(false);
  const [deleteLibraryFiles, setDeleteLibraryFiles] = useState(false);
  const [busy, setBusy] = useState(false);

  // 每次打开都重新拉预览并复位开关：上次勾过的"连媒体库一起删"绝不能
  // 在下一次取消订阅时被默认带上
  useEffect(() => {
    if (!open) return;
    setPreview(null);
    setDeleteTorrents(false);
    setDeleteLibraryFiles(false);
    let alive = true;
    getSubscriptionRemovalPreview(subscriptionId)
      .then((data) => {
        if (alive) setPreview(data);
      })
      .catch(() => {
        // 预览失败不挡住取消订阅本身：开关保持禁用，用户仍可只取消订阅
        if (alive) setPreview(null);
      });
    return () => {
      alive = false;
    };
  }, [open, subscriptionId]);

  const submit = async () => {
    setBusy(true);
    try {
      await onConfirm({ deleteTorrents, deleteLibraryFiles });
    } finally {
      setBusy(false);
    }
  };

  const torrentCount = preview?.torrent_count ?? 0;
  const fileCount = preview?.library_file_count ?? 0;
  const retention = preview?.recycle_retention_days ?? 7;

  return (
    <Modal
      open={open}
      onClose={busy ? () => {} : onClose}
      label={`取消订阅《${title}》`}
      raised={raised}
    >
      <div className="p-6 max-md:p-5">
        <h2 className="text-title-sm font-bold text-white">取消订阅《{title}》？</h2>
        <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
          将停止追踪剩余内容。默认只取消订阅，已经下载或入库的内容都会保留。
        </p>

        <div className="mt-4 space-y-3">
          <CleanupToggle
            checked={deleteTorrents}
            disabled={preview === null || torrentCount === 0}
            onChange={setDeleteTorrents}
            label={
              preview === null
                ? "同时删除相关的下载任务"
                : torrentCount === 0
                  ? "同时删除相关的下载任务（没有可删除的任务）"
                  : `同时删除相关的下载任务（${torrentCount} 个）`
            }
            description={
              torrentCount === 0
                ? undefined
                : "从下载器移除任务，并删除下载目录里的文件，不可恢复。"
            }
            warning={
              deleteTorrents && (preview?.hit_and_run_count ?? 0) > 0
                ? `其中 ${preview?.hit_and_run_count} 个种子仍在 H&R 考核或考核状态未知，删除后可能影响站点考核。`
                : undefined
            }
          />
          <CleanupToggle
            checked={deleteLibraryFiles}
            disabled={preview === null || fileCount === 0}
            onChange={setDeleteLibraryFiles}
            label={
              preview === null
                ? "同时删除媒体库里的资源"
                : fileCount === 0
                  ? "同时删除媒体库里的资源（媒体库中没有该作品）"
                  : `同时删除媒体库里的资源（${fileCount} 个文件 · ${formatBytes(preview.library_bytes)}）`
            }
            description={
              fileCount === 0
                ? undefined
                : `文件会移入媒体库回收站，${retention} 天内可以恢复。`
            }
          />
        </div>

        {(deleteTorrents || deleteLibraryFiles) && (
          <p className="mt-4 rounded-lg bg-white/[0.04] px-3 py-2.5 text-xs leading-5 text-[var(--text-muted)]">
            订阅会立刻取消，清理在后台进行——可以在「任务中心」查看进度和结果。
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2.5">
          <button type="button" onClick={onClose} disabled={busy} className={CANCEL_BTN_CLS}>
            先不
          </button>
          <button type="button" onClick={submit} disabled={busy} className={DANGER_BTN_CLS}>
            {busy ? "处理中…" : "取消订阅"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 一个清理开关：标题 + 后果说明 +（勾上后才出现的）风险警示。 */
function CleanupToggle({
  checked,
  disabled,
  onChange,
  label,
  description,
  warning,
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (value: boolean) => void;
  label: string;
  description?: string;
  warning?: string;
}) {
  return (
    <label
      className={`flex items-start gap-2.5 ${disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer"}`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 size-4 shrink-0 accent-[var(--accent-2)]"
      />
      <span className="min-w-0">
        <span className="block text-sub leading-6 text-white/90">{label}</span>
        {description && (
          <span className="block text-xs leading-5 text-[var(--text-muted)]">{description}</span>
        )}
        {warning && (
          <span className="mt-1 block text-xs leading-5 text-[var(--danger)]">{warning}</span>
        )}
      </span>
    </label>
  );
}
