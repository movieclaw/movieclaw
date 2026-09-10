"use client";

import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/feedback";
import {
  addCollectionItems,
  createCollection,
  listCollections,
  type Collection,
} from "@/lib/api/collections";

/**
 * 「加入合集」弹层（docs/design/library-filtering.md F4）。
 *
 * **只列名单驱动的合集。** 规则驱动的合集成员是条件求值出来的，往里手工塞
 * 一部片会静默消失（``resolve_members`` 对它压根不看 ``collection_item``）；
 * 服务端会拒绝，但更好的做法是**根本不把它摆出来**——一个点了会报错的选项
 * 比没有这个选项更糟。
 *
 * 所以这里同时给「新建一个合集」：用户十有八九还没有手动合集（自建的那几个
 * 都是「筛完存为合集」存下来的规则合集），只列出来给他看一个空列表等于死路。
 */
export function AddToCollectionDialog({
  libraryId,
  mediaItemId,
  title,
  onClose,
}: {
  libraryId: number;
  mediaItemId: number;
  /** 作品名，只用于文案 */
  title: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<Collection[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    listCollections({ libraryId, includeEmpty: true })
      // 名单驱动 = 不会自己长的那些。内置与系列合集同样排除（它们都是规则驱动）
      .then((all) => alive && setRows(all.filter((row) => !row.rule_driven)))
      .catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, [libraryId]);

  const addTo = useCallback(
    async (collection: Collection) => {
      setBusy(true);
      try {
        await addCollectionItems(collection.id, [mediaItemId]);
        toast.success(`《${title}》已加入「${collection.name}」`);
        onClose();
      } catch (error) {
        toast.error((error as Error).message);
        setBusy(false);
      }
    },
    [mediaItemId, onClose, title, toast],
  );

  const createAndAdd = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      // item_ids 直接带上这一部：新建 + 加入是一次请求，不用先建空合集再加
      const created = await createCollection({
        name: trimmed,
        library_id: libraryId,
        item_ids: [mediaItemId],
      });
      toast.success(`已新建「${created.name}」并加入《${title}》`);
      onClose();
    } catch (error) {
      toast.error((error as Error).message);
      setBusy(false);
    }
  }, [libraryId, mediaItemId, name, onClose, title, toast]);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4 max-md:items-end max-md:p-0"
      onClick={onClose}
    >
      <div
        className="menu-surface w-full max-w-sm rounded-2xl p-4 max-md:rounded-b-none max-md:pb-8"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="text-ui font-semibold text-[var(--text-strong)]">加入合集</h2>
        <p className="mt-1 text-sub text-[var(--text-faint)]">
          手动合集不会自动收录新片，加进去的就是你挑的那些。
        </p>

        <div className="mt-3 flex max-h-64 flex-col gap-1 overflow-y-auto">
          {rows === null ? (
            <p className="py-6 text-center text-sub text-[var(--text-faint)]">正在读取合集…</p>
          ) : rows.length === 0 ? (
            <p className="py-4 text-center text-sub text-[var(--text-faint)]">
              还没有手动合集。下面新建一个。
            </p>
          ) : (
            rows.map((row) => (
              <button
                key={row.id}
                type="button"
                disabled={busy}
                onClick={() => void addTo(row)}
                className="glass-row flex items-center justify-between rounded-lg px-3 py-2 text-left text-ui transition hover:bg-[var(--glass-fill-hover)] disabled:opacity-40"
              >
                <span className="truncate text-[var(--text-strong)]">{row.name}</span>
                <span className="shrink-0 text-caption text-[var(--text-faint)]">
                  {row.item_count} 部
                </span>
              </button>
            ))
          )}
        </div>

        <div className="mt-3 flex gap-2 border-t border-white/[0.07] pt-3">
          <input
            type="text"
            value={name}
            placeholder="新建合集，取个名字"
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void createAndAdd();
            }}
            className="h-9 min-w-0 flex-1 rounded-lg bg-white/[0.05] px-3 text-ui text-[var(--text-strong)] outline-none placeholder:text-white/30 focus:bg-white/[0.08]"
          />
          <button
            type="button"
            disabled={busy || !name.trim()}
            onClick={() => void createAndAdd()}
            className="h-9 shrink-0 rounded-lg bg-white/10 px-3 text-ui font-medium text-white transition hover:bg-white/20 disabled:opacity-40"
          >
            新建并加入
          </button>
        </div>
      </div>
    </div>
  );
}
