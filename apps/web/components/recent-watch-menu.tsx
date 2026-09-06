"use client";

/**
 * 媒体库首页「最近观看」标题右侧的 ⋯ 菜单：清空观看记录的**唯一**入口。
 *
 * 以前这个入口挂在每个单库页的 ⋯ 菜单里（「清空我的观看记录」）——进任何
 * 一个库都能看到一条与浏览无关的破坏性操作，位置也不对：观看记录是跨库
 * 的个人数据，它该长在展示这些记录的地方。这里按时间与范围给四种清法：
 * 今天 / 最近一周 / 全部 / 某个媒体库（弹窗里下拉选库）。
 *
 * 四条都是删自己的记录（docs/design/library-access.md 2.6），二次确认后
 * 调同一个接口；成功后由父组件重新拉最近观看，这一行随之刷新或整段隐藏。
 */

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useState } from "react";

import { useConfirm, useToast } from "@/components/feedback";
import { MoreIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import type { MediaLibrary } from "@/lib/api/libraries";
import { clearPlaybackHistory } from "@/lib/api/playback";

const ITEM_CLASS =
  "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
  "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)]";

/** 「今天」按浏览器本地日历算：从当地 0 点起，不是过去 24 小时。 */
function startOfToday(): Date {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  return start;
}

function weekAgo(): Date {
  return new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
}

export function RecentWatchMenu({
  libraries,
  onCleared,
}: {
  /** 当前身份可浏览的库：「清空某个媒体库」的下拉候选 */
  libraries: MediaLibrary[];
  /** 任一清除成功后回调，父组件据此重新拉最近观看 */
  onCleared: () => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [pickingLibrary, setPickingLibrary] = useState(false);

  /** 按时间窗口 / 全部清除：确认 → 调接口 → 回执。 */
  const clearRange = (
    label: string,
    since: Date | null,
    description: string,
  ) => {
    void confirm({
      title: `清空${label}？`,
      description,
      confirmLabel: "清空",
      tone: "danger",
    }).then((ok) => {
      if (!ok) return;
      clearPlaybackHistory("all", since ? { since } : {})
        .then(({ result, message }) => {
          // 时间窗口的回执由前端组句：服务端不知道「这段时间」是今天还是一周
          toast.success(
            since
              ? result.deleted_states > 0
                ? `已清空${label}`
                : `${label}里没有可清除的记录`
              : message,
          );
          onCleared();
        })
        .catch((e) => toast.error((e as Error).message));
    });
  };

  return (
    <>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            aria-label="清空观看记录"
            className="flex size-7 items-center justify-center rounded-full text-[var(--text-muted)] outline-none transition hover:bg-white/[0.08] hover:text-[var(--text)] focus-visible:ring-2 focus-visible:ring-white/60 data-[state=open]:bg-white/[0.1] data-[state=open]:text-[var(--text)]"
          >
            <MoreIcon className="size-[18px]" />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={6}
            collisionPadding={12}
            className="menu-surface z-50 min-w-[13rem] p-1"
          >
            <DropdownMenu.Item
              onSelect={() =>
                clearRange(
                  "今天的观看记录",
                  startOfToday(),
                  "今天播放过的作品，续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。",
                )
              }
              className={ITEM_CLASS}
            >
              清空今天的观看记录…
            </DropdownMenu.Item>
            <DropdownMenu.Item
              onSelect={() =>
                clearRange(
                  "最近一周的观看记录",
                  weekAgo(),
                  "最近 7 天播放过的作品，续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。",
                )
              }
              className={ITEM_CLASS}
            >
              清空最近一周的观看记录…
            </DropdownMenu.Item>
            <DropdownMenu.Item
              onSelect={() =>
                clearRange(
                  "全部观看记录",
                  null,
                  "所有作品的续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录；应用更新前的自动备份仍包含历史记录。",
                )
              }
              className={ITEM_CLASS}
            >
              清空全部观看记录…
            </DropdownMenu.Item>
            <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
            <DropdownMenu.Item onSelect={() => setPickingLibrary(true)} className={ITEM_CLASS}>
              清空某个媒体库的观看记录…
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      {pickingLibrary && (
        <ClearLibraryHistoryDialog
          libraries={libraries}
          onClose={() => setPickingLibrary(false)}
          onCleared={onCleared}
        />
      )}
    </>
  );
}

/**
 * 「清空某个媒体库」：弹窗里下拉选库再确认。弹窗本身就是二次确认——选库
 * 这一步已经要求用户明确目标，再叠一层 confirm 只会多点一次。
 */
function ClearLibraryHistoryDialog({
  libraries,
  onClose,
  onCleared,
}: {
  libraries: MediaLibrary[];
  onClose: () => void;
  onCleared: () => void;
}) {
  const toast = useToast();
  const [libraryId, setLibraryId] = useState<number | null>(libraries[0]?.id ?? null);
  const [busy, setBusy] = useState(false);
  const selected = libraries.find((library) => library.id === libraryId) ?? null;

  const submit = () => {
    if (libraryId === null || busy) return;
    setBusy(true);
    clearPlaybackHistory("library", { libraryId })
      .then(({ message }) => {
        toast.success(message);
        onCleared();
        onClose();
      })
      .catch((e) => toast.error((e as Error).message))
      .finally(() => setBusy(false));
  };

  return (
    <Modal open onClose={onClose} label="清空某个媒体库的观看记录">
      <form
        className="p-6 max-md:p-5"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2 className="text-title-sm font-bold text-white">清空某个媒体库的观看记录</h2>
        <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
          选中库里所有作品的续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。
        </p>
        <label className="mt-4 block">
          <span className="mb-1.5 block text-sub font-medium text-white/85">媒体库</span>
          <select
            value={libraryId === null ? "" : String(libraryId)}
            onChange={(e) => setLibraryId(e.target.value === "" ? null : Number(e.target.value))}
            disabled={libraries.length === 0}
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 disabled:opacity-50 [&>option]:bg-[#181c28]"
          >
            {libraries.length === 0 && <option value="">没有可浏览的媒体库</option>}
            {libraries.map((library) => (
              <option key={library.id} value={library.id}>
                {library.name}
              </option>
            ))}
          </select>
        </label>
        <div className="mt-5 flex justify-end gap-2.5">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1]"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={selected === null || busy}
            className="rounded-lg bg-red-500/85 px-4 py-2 text-ui font-medium text-white transition hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "清空中…" : selected ? `清空「${selected.name}」` : "清空"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
