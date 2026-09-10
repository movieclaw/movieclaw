"use client";

import { useCallback, useState } from "react";

import { useToast } from "@/components/feedback";
import { removeCollectionItem, reorderCollectionItems } from "@/lib/api/collections";
import type { LibraryItem } from "@/lib/api/libraries";

/**
 * 手动合集的「整理顺序」面板（docs/design/library-filtering.md F4）。
 *
 * **为什么不在海报墙上直接拖**：那面墙是虚拟化的（只渲染视口内的格子），
 * 拖到列表外要靠自动滚动 + 重新挂载被回收的格子，做出来既脆又难在手机上用。
 * 换成一份**不虚拟化的短名单**：手动合集本来就是"我挑的这十几二十部"，
 * 一屏多一点就到底了，拖起来还稳。
 *
 * 顺序落到 ``collection_item.position``，而海报墙、Jellyfin、分享页三处都走
 * ``resolve_members()``——名单驱动那一支就是按 position 取的，所以三处天然一致，
 * 不需要在任何一处再排一次。
 */
export function CollectionOrderPanel({
  collectionId,
  items,
  onClose,
  onSaved,
}: {
  collectionId: number;
  items: LibraryItem[];
  onClose: () => void;
  /** 保存或移出之后让详情页重新取一遍成员 */
  onSaved: () => void;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<LibraryItem[]>(items);
  const [dragging, setDragging] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const move = useCallback((from: number, to: number) => {
    setRows((prev) => {
      if (to < 0 || to >= prev.length || from === to) return prev;
      const next = [...prev];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  }, []);

  const save = useCallback(async () => {
    setBusy(true);
    try {
      await reorderCollectionItems(
        collectionId,
        rows.map((row) => row.media_item_id),
      );
      toast.success("顺序已保存");
      onSaved();
      onClose();
    } catch (error) {
      toast.error((error as Error).message);
      setBusy(false);
    }
  }, [collectionId, onClose, onSaved, rows, toast]);

  const drop = useCallback(
    async (mediaItemId: number) => {
      setBusy(true);
      try {
        await removeCollectionItem(collectionId, mediaItemId);
        setRows((prev) => prev.filter((row) => row.media_item_id !== mediaItemId));
        onSaved();
      } catch (error) {
        toast.error((error as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [collectionId, onSaved, toast],
  );

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4 max-md:items-end max-md:p-0"
      onClick={onClose}
    >
      <div
        className="menu-surface flex max-h-[80vh] w-full max-w-md flex-col rounded-2xl p-4 max-md:rounded-b-none max-md:pb-8"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="text-ui font-semibold text-[var(--text-strong)]">整理顺序</h2>
        <p className="mt-1 text-sub text-[var(--text-faint)]">
          拖动调整先后；顺序在网页、播放器和分享页三处一致。移出去只是从名单里
          去掉，影片一部都不会少。
        </p>

        <div className="mt-3 flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
          {rows.map((row, index) => (
            <div
              key={row.media_item_id}
              draggable
              onDragStart={() => setDragging(index)}
              onDragOver={(event) => {
                event.preventDefault();
                if (dragging !== null && dragging !== index) {
                  move(dragging, index);
                  setDragging(index);
                }
              }}
              onDragEnd={() => setDragging(null)}
              className={`glass-row flex items-center gap-2 rounded-lg px-2 py-2 ${
                dragging === index ? "opacity-50" : ""
              }`}
            >
              {/* 拖柄：整行可拖，但要有一个明确的"这里能拖"的信号 */}
              <span aria-hidden className="cursor-grab select-none px-1 text-white/30">
                ⠿
              </span>
              <span className="w-6 shrink-0 text-right font-mono tabular-nums text-caption text-white/35">
                {index + 1}
              </span>
              <span className="min-w-0 flex-1 truncate text-ui text-[var(--text-strong)]">
                {row.title}
                {row.year ? <span className="ml-1.5 text-white/35">{row.year}</span> : null}
              </span>
              {/* 手机上拖不顺手，补一对上下键——同一份状态，两种操作方式 */}
              <button
                type="button"
                aria-label="上移"
                disabled={index === 0}
                onClick={() => move(index, index - 1)}
                className="size-7 shrink-0 rounded text-white/50 transition hover:bg-white/10 hover:text-white disabled:opacity-20"
              >
                ↑
              </button>
              <button
                type="button"
                aria-label="下移"
                disabled={index === rows.length - 1}
                onClick={() => move(index, index + 1)}
                className="size-7 shrink-0 rounded text-white/50 transition hover:bg-white/10 hover:text-white disabled:opacity-20"
              >
                ↓
              </button>
              <button
                type="button"
                aria-label={`移出 ${row.title}`}
                disabled={busy}
                onClick={() => void drop(row.media_item_id)}
                className="size-7 shrink-0 rounded text-white/40 transition hover:bg-white/10 hover:text-[var(--danger,#ff6b6b)] disabled:opacity-20"
              >
                ✕
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <p className="py-6 text-center text-sub text-[var(--text-faint)]">
              这个合集现在一部都没有。
            </p>
          )}
        </div>

        <div className="mt-3 flex justify-end gap-2 border-t border-white/[0.07] pt-3">
          <button
            type="button"
            onClick={onClose}
            className="h-9 rounded-lg px-3 text-ui text-white/70 transition hover:bg-white/10 hover:text-white"
          >
            取消
          </button>
          <button
            type="button"
            disabled={busy || rows.length === 0}
            onClick={() => void save()}
            className="h-9 rounded-lg bg-white/10 px-3 text-ui font-medium text-white transition hover:bg-white/20 disabled:opacity-40"
          >
            保存顺序
          </button>
        </div>
      </div>
    </div>
  );
}
