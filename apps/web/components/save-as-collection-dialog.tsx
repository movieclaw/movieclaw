"use client";

import { useEffect, useMemo, useState } from "react";

import { useToast } from "@/components/feedback";
import { Modal } from "@/components/modal";
import { createCollection, type Collection } from "@/lib/api/collections";
import {
  getLibraryFacets,
  type LibraryFacets,
  type LibraryFilter,
} from "@/lib/api/libraries";
import { filterToRules } from "@/lib/library-filter";

/**
 * 「筛完存为合集」（docs/design/library-filtering.md 4.2）。
 *
 * 合集不是一个新概念，是**存好的筛选**——所以这个弹窗要做的只有两件事：
 * 起个名字，以及问清楚一件用户真正会在意的事：
 *
 *   自动收录  ——「以后符合这组条件的片都算」（规则驱动）
 *   固定这批  ——「就现在这些，别再变了」（名单驱动）
 *
 * 这两者的差别在几个月后才显形（多了/少了片），事后无从追查，所以必须在
 * 创建这一刻问，而且用后果的语言问，不能写成「smart / manual」让用户自己猜。
 *
 * 「固定这批」由服务端定格（payload.snapshot）：客户端只表达意图，不用把
 * 上千个 id 拉下来再传回去。
 */
export function SaveAsCollectionDialog({
  open,
  libraryId,
  filter,
  onClose,
  onCreated,
}: {
  open: boolean;
  libraryId: number;
  /** 要存下来的那组条件（即此刻墙上生效的筛选） */
  filter: LibraryFilter;
  onClose: () => void;
  onCreated: (collection: Collection) => void;
}) {
  const toast = useToast();
  const [facets, setFacets] = useState<LibraryFacets | null>(null);
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const [snapshot, setSnapshot] = useState(false);
  const [privateOnly, setPrivateOnly] = useState(false);
  const [saving, setSaving] = useState(false);

  // 命中数与取值的中文名都来自 facet：与筛选条上显示的是同一份口径，
  // 弹窗里写「筛出 42 部」而墙上是 39 部这种事不可能发生
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setFacets(null);
    getLibraryFacets(libraryId, filter, "all")
      .then((data) => alive && setFacets(data))
      .catch(() => alive && setFacets(null));
    return () => {
      alive = false;
    };
  }, [open, libraryId, filter]);

  /** 建议名：把条件本身念出来（「动画 · 日本」），比「新建合集 3」有用得多。 */
  const suggested = useMemo(() => summarize(filter, facets), [filter, facets]);

  // 用户没动过输入框就跟着建议名走；一旦手动改过就不再覆盖他的输入
  useEffect(() => {
    if (!touched) setName(suggested);
  }, [suggested, touched]);

  // 每次重新打开都从干净状态开始——上一次的残留会让人以为在改旧合集
  useEffect(() => {
    if (open) return;
    setTouched(false);
    setSnapshot(false);
    setPrivateOnly(false);
  }, [open]);

  const rules = useMemo(() => filterToRules(filter), [filter]);
  const total = facets?.total ?? null;

  const submit = async () => {
    const finalName = name.trim() || suggested;
    if (!finalName) {
      toast.error("给这个合集起个名字");
      return;
    }
    setSaving(true);
    try {
      const created = await createCollection({
        name: finalName,
        library_id: libraryId,
        rules,
        snapshot,
        visibility: privateOnly ? "private" : "household",
      });
      toast.success(`已存为合集「${created.name}」`);
      onCreated(created);
      onClose();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "存合集失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} label="存为合集" width="md">
      <div className="p-5 max-md:p-4">
        <h2 className="text-body-lg font-semibold text-[var(--text-strong)]">存为合集</h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">
          {total === null ? "正在数当前条件命中多少部…" : `当前条件命中 ${total} 部`}
        </p>

        <label className="mt-4 block">
          <span className="text-caption text-[var(--text-faint)]">名字</span>
          <input
            value={name}
            autoFocus
            onChange={(e) => {
              setTouched(true);
              setName(e.target.value);
            }}
            placeholder={suggested || "合集名"}
            className="mt-1 h-9 w-full rounded-lg bg-white/[0.06] px-3 text-ui text-[var(--text-strong)] outline-none ring-1 ring-white/10 focus:ring-white/25"
          />
        </label>

        <div className="mt-4 space-y-2">
          <ModeOption
            selected={!snapshot}
            onSelect={() => setSnapshot(false)}
            title="自动收录"
            detail="以后新入库的片，只要符合这组条件就会自己进来。"
          />
          <ModeOption
            selected={snapshot}
            onSelect={() => setSnapshot(true)}
            title={total === null ? "固定现在这批" : `固定现在这 ${total} 部`}
            detail="就留下此刻这些，之后不再变化。"
          />
        </div>

        <label className="mt-4 flex items-center gap-2 text-ui text-[var(--text-muted)]">
          <input
            type="checkbox"
            checked={privateOnly}
            onChange={(e) => setPrivateOnly(e.target.checked)}
            className="size-4 accent-white/80"
          />
          只有我可见
        </label>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="h-9 rounded-full px-4 text-ui text-[var(--text-muted)] hover:bg-white/10"
          >
            取消
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={submit}
            className="btn-glass h-9 px-4 text-ui font-medium disabled:opacity-50"
          >
            {saving ? "保存中…" : "存为合集"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 两种形态各占一整块，标题说做法、副行说后果——差别在几个月后才显形，得说清楚。 */
function ModeOption({
  selected,
  onSelect,
  title,
  detail,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  detail: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`block w-full rounded-xl px-3 py-2.5 text-left ring-1 transition ${
        selected
          ? "bg-white/[0.10] ring-white/25"
          : "bg-white/[0.03] ring-white/[0.06] hover:bg-white/[0.06]"
      }`}
    >
      <span className="text-ui font-medium text-[var(--text-strong)]">{title}</span>
      <span className="mt-0.5 block text-sub text-[var(--text-faint)]">{detail}</span>
    </button>
  );
}

/**
 * 条件 → 一句人话（建议名）。取值的中文名来自 facet，与筛选条上是同一份。
 *
 * **认不出的取值直接跳过**，绝不把裸值填进输入框：facet 还没到时它是
 * 「878」「JP」这种东西，而用户手一快就会存下一个叫「878」的合集。跳过的
 * 结果是建议名先空着（输入框显示占位），facet 一到就自己补上。
 */
function summarize(filter: LibraryFilter, facets: LibraryFacets | null): string {
  const labelOf = (pool: { value: string; label: string }[] | undefined, value: string) =>
    pool?.find((row) => row.value === value)?.label;
  const parts: (string | undefined)[] = [];
  for (const id of filter.genres ?? []) parts.push(labelOf(facets?.genres, String(id)));
  for (const code of filter.countries ?? []) parts.push(labelOf(facets?.countries, code));
  for (const decade of filter.decades ?? []) parts.push(labelOf(facets?.decades, decade));
  if (filter.watch) parts.push(labelOf(facets?.watch, filter.watch));
  if (filter.ratingGte !== undefined && filter.ratingGte !== null) {
    parts.push(`${filter.ratingGte} 分以上`);
  }
  // 画质的取值本身就是人话（"2160p"），不必等 facet
  for (const value of filter.resolutions ?? []) parts.push(value);
  return parts.filter(Boolean).slice(0, 3).join(" · ");
}
