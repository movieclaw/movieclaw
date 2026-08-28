"use client";

/**
 * 发现页页脚的院线地区切换（docs/design/scrape-customization.md §2.1）。
 *
 * 形态照 Google 的地区切换：页脚一行浅色小字「院线地区：中国大陆」，
 * 点地区名弹出紧凑菜单，**选择即自动保存**、无保存按钮——配置放在离它
 * 影响的内容最近的地方，改完同屏就能看到「正在热映 / 即将上映」刷新。
 * 存在感刻意压到最低，不打扰浏览。
 *
 * 非管理员只读展示（后端 PUT 挂 require_admin）。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/components/feedback";
import { getDiscoverRegion, setDiscoverRegion } from "@/lib/api/scrape";

/** 常用院线地区。取值是 ISO 3166-1，与后端 TMDB region 参数一致。 */
const REGIONS: { code: string; name: string }[] = [
  { code: "CN", name: "中国大陆" },
  { code: "HK", name: "香港" },
  { code: "TW", name: "台湾" },
  { code: "US", name: "美国" },
  { code: "JP", name: "日本" },
  { code: "KR", name: "韩国" },
  { code: "GB", name: "英国" },
];

function regionName(code: string): string {
  return REGIONS.find((r) => r.code === code)?.name ?? code;
}

export function DiscoverRegionFooter({ onChanged }: { onChanged?: () => void }) {
  const toast = useToast();
  const [region, setRegion] = useState<string | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let alive = true;
    // 拉不到就整行不渲染：页脚是锦上添花，不该因它报错打扰浏览
    getDiscoverRegion()
      .then((view) => {
        if (!alive) return;
        setRegion(view.region);
        setCanEdit(view.can_edit);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  const pick = useCallback(
    async (code: string) => {
      setOpen(false);
      if (code === region) return;
      const previous = region;
      setRegion(code); // 乐观更新：菜单一收起就看到新地区名
      try {
        const view = await setDiscoverRegion(code);
        setRegion(view.region);
        setSaved(true);
        window.setTimeout(() => setSaved(false), 1800);
        onChanged?.();
      } catch (err) {
        setRegion(previous);
        toast.error(err instanceof Error ? err.message : "切换地区失败，请重试");
      }
    },
    [region, onChanged, toast],
  );

  if (region === null) return null;

  return (
    <div
      ref={wrapRef}
      className="relative mt-6 flex items-center justify-center gap-1.5 border-t border-white/[0.08] px-6 pt-3.5 text-caption text-[var(--text-faint)] max-md:px-4"
    >
      <span>院线地区：</span>
      {canEdit ? (
        <button
          type="button"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={(e) => {
            e.stopPropagation();
            setOpen((v) => !v);
          }}
          className="rounded-md border-b border-dashed border-white/[0.15] px-1 py-0.5 font-medium text-[var(--text-muted)] transition-colors hover:bg-white/[0.06] hover:text-[var(--text)]"
        >
          {regionName(region)}
        </button>
      ) : (
        <span className="font-medium text-[var(--text-muted)]">{regionName(region)}</span>
      )}
      {saved && <span className="text-[var(--ok)]">✓ 已保存</span>}
      {open && (
        <div
          role="menu"
          className="absolute bottom-[calc(100%+8px)] left-1/2 z-20 min-w-[150px] -translate-x-1/2 rounded-xl border border-white/[0.15] bg-[var(--surface-raised)] p-1.5 shadow-[0_14px_40px_rgba(0,0,0,0.5)] backdrop-blur-xl"
        >
          {REGIONS.map((item) => (
            <button
              key={item.code}
              type="button"
              role="menuitem"
              onClick={() => void pick(item.code)}
              className={`flex w-full items-center justify-between gap-2.5 rounded-lg px-3 py-1.5 text-left text-sub transition-colors hover:bg-white/[0.07] ${
                item.code === region
                  ? "font-semibold text-[var(--text)]"
                  : "text-[var(--text-muted)]"
              }`}
            >
              {item.name}
              {item.code === region && <span className="text-[var(--accent-2)]">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
