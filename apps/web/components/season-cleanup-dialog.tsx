"use client";

/**
 * 减季后的清理追问弹窗（管理员）。
 *
 * 「调整订阅」里取消勾选某一季，保存本身不动任何内容——这是减季一直以来的语义，
 * 不能改。但用户此前想让那一季从机器上消失，得自己去下载器找到那几个种子、再去
 * 媒体库逐个删，两处都不好找。于是在减季保存成功之后追问一次，把两件事收成这里
 * 的两个显式勾选。
 *
 * 与「取消订阅」弹窗（subscription-cancel-dialog.tsx）共用同一套视觉与后果说明，
 * 三处有意不同：
 *
 * 1. **可逆性**：减季本身可逆（重新勾选会恢复追踪），删种子不可逆——种子那条必须
 *    写明"重新勾选需要重新下载"，不能照抄取消订阅的文案；
 * 2. **跨季种子**：整季包/全剧包只要还覆盖着保留的季就不会删，这件事要主动说出来
 *    ——否则用户以为"删了 N 个任务"就干净了，回头发现还在做种；
 * 3. **走开的按钮叫「保留内容」**：此刻季已经减完了，叫「取消」会被读成"撤销减季"，
 *    而那正是用户此刻最可能的误解。
 *
 * 数量由后端 removal-preview（带 seasons 参数）给出，调用方保存后先拉好再打开本
 * 弹窗；两条清单都为空时根本不该打开——没数字的勾选框等于让用户盲签。
 */

import { useState } from "react";

import { Modal } from "@/components/modal";
import { formatBytes } from "@/lib/format";
import type {
  SubscriptionRemovalOptions,
  SubscriptionRemovalPreview,
} from "@/lib/api/subscriptions";

const CANCEL_BTN_CLS =
  "rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1]";
const DANGER_BTN_CLS =
  "rounded-lg bg-red-500/85 px-4 py-2 text-ui font-medium text-white transition hover:bg-red-500 disabled:opacity-50";

/** 「第 2 季」/「第 2、4 季」——与后端 _season_text 同一口径。 */
function seasonText(seasons: number[]): string {
  return `第 ${[...seasons].sort((a, b) => a - b).join("、")} 季`;
}

export function SeasonCleanupDialog({
  open,
  title,
  seasons,
  preview,
  onClose,
  onConfirm,
}: {
  open: boolean;
  /** 作品名 */
  title: string;
  /** 本次移出订阅范围的季号 */
  seasons: number[];
  /** 调用方保存成功后已拉好的预览（两条清单都为空时不应打开本弹窗） */
  preview: SubscriptionRemovalPreview;
  /** 用户选择「保留内容」或关窗：什么都不清理 */
  onClose: () => void;
  /** 用户确认清理：带上两个开关的最终取值 */
  onConfirm: (options: SubscriptionRemovalOptions) => Promise<void> | void;
}) {
  const [deleteTorrents, setDeleteTorrents] = useState(false);
  const [deleteLibraryFiles, setDeleteLibraryFiles] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await onConfirm({ deleteTorrents, deleteLibraryFiles });
    } finally {
      setBusy(false);
    }
  };

  const label = seasonText(seasons);
  // 多季一起减时「这一季」读不通；范围本身已由标题和每个开关点明
  const them = seasons.length > 1 ? "这几季" : "这一季";
  const torrentCount = preview.torrent_count;
  const fileCount = preview.library_file_count;
  const retained = preview.retained_cross_season;

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} label={`清理${label}的内容`}>
      <div className="p-6 max-md:p-5">
        <h2 className="text-title-sm font-bold text-white">{label}已移出订阅</h2>
        <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
          《{title}》的{them}不再追了。要不要把已经下载的内容也清理掉？不清理也没关系，
          种子和文件都原样留着。
        </p>

        <div className="mt-4 space-y-3">
          <CleanupToggle
            checked={deleteTorrents}
            disabled={torrentCount === 0}
            onChange={setDeleteTorrents}
            label={
              torrentCount === 0
                ? `同时删除${label}的下载任务（没有可单独删除的任务）`
                : `同时删除${label}的下载任务（${torrentCount} 个）`
            }
            description={
              torrentCount === 0
                ? undefined
                : `从下载器移除任务，并删除下载目录里的文件，不可恢复。以后重新勾选${them}需要重新下载。`
            }
            warning={
              deleteTorrents && preview.hit_and_run_count > 0
                ? `其中 ${preview.hit_and_run_count} 个种子仍在 H&R 考核或考核状态未知，删除后可能影响站点考核。`
                : undefined
            }
          />
          <CleanupToggle
            checked={deleteLibraryFiles}
            disabled={fileCount === 0}
            onChange={setDeleteLibraryFiles}
            label={
              fileCount === 0
                ? `同时删除${label}在媒体库里的资源（媒体库中没有）`
                : `同时删除${label}在媒体库里的资源（${fileCount} 个文件 · ${formatBytes(preview.library_bytes)}）`
            }
            description={
              fileCount === 0
                ? undefined
                : `文件会移入媒体库回收站，${preview.recycle_retention_days} 天内可以恢复。`
            }
          />
        </div>

        {/* 被保护的跨季种子：信息而非警告——系统正确地留下了别的季还要用的东西，
            用户不需要处理它（全站口径：只有"要你处理"才配用警告色） */}
        {retained.length > 0 && (
          <p className="mt-3 rounded-lg border border-[var(--info)]/25 bg-[var(--info)]/[0.08] px-3 py-2 text-xs leading-5 text-[#bcd4ff]">
            另有 {retained.length} 个跨季种子
            {retained[0].seasons.length > 0 && `（如 ${seasonText(retained[0].seasons)}合集）`}
            仍被保留的季使用，不会删除。
          </p>
        )}

        {(deleteTorrents || deleteLibraryFiles) && (
          <p className="mt-4 rounded-lg bg-white/[0.04] px-3 py-2.5 text-xs leading-5 text-[var(--text-muted)]">
            清理在后台进行——可以在「任务中心」查看进度和结果。
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2.5">
          <button type="button" onClick={onClose} disabled={busy} className={CANCEL_BTN_CLS}>
            保留内容
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={busy || (!deleteTorrents && !deleteLibraryFiles)}
            className={DANGER_BTN_CLS}
          >
            {busy ? "处理中…" : `清理${label}`}
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
