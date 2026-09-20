"use client";

import { useCallback, useEffect, useState } from "react";
import type { Route } from "next";
import Link from "next/link";

import { BrandLoader } from "@/components/brand-loader";
import { ArrowLeftIcon } from "@/components/icons";
import { PageNav } from "@/components/page-nav";
import { PosterCardVisual } from "@/components/poster-card";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import {
  fetchDiscoveredPersonDetails,
  type DiscoveredPersonDetailsData,
} from "@/lib/api/discover";
import { HttpError } from "@/lib/http";
import { useMediaDetail } from "@/lib/media-detail";
import type { MediaItem } from "@/lib/media-types";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 发现页影人详情：展示 TMDB combined credits 中的完整影视履历。
 *
 * 它与 `/people/[id]` 的库内人物页职责不同：后者只回答“我库里有哪些作品”，
 * 本页则不因本地库存裁剪 TMDB 履历，只在海报右上角叠加精确匹配出的关系状态。
 */
export function DiscoveredPersonDetailView({
  tmdbPersonId,
}: {
  tmdbPersonId: number | string;
}) {
  const [person, setPerson] = useState<DiscoveredPersonDetailsData | null>(null);
  const [failure, setFailure] = useState<"missing" | "error" | null>(null);
  const { subscriptionOf } = useSubscribeEntry();
  const { open } = useMediaDetail();
  const navFallback = { label: "发现电影", href: "/discover/movie" as Route };

  useEffect(() => {
    let cancelled = false;
    setPerson(null);
    setFailure(null);
    fetchDiscoveredPersonDetails(tmdbPersonId)
      .then((data) => {
        if (!cancelled) setPerson(data);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setFailure(error instanceof HttpError && error.status === 404 ? "missing" : "error");
      });
    return () => {
      cancelled = true;
    };
  }, [tmdbPersonId]);

  usePageTitle(person?.name);

  const isSubscribed = useCallback(
    (item: MediaItem) => Boolean(subscriptionOf(item)),
    [subscriptionOf],
  );

  if (failure !== null) {
    return (
      <div className="flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <PersonFallback failure={failure} />
      </div>
    );
  }
  if (person === null) {
    return (
      <div className="flex h-full flex-col">
        <PageNav title="" fallback={navFallback} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <BrandLoader className="size-5" />
          正在读取 TMDB 影人作品…
        </div>
      </div>
    );
  }

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto pb-12">
      <PageNav title={person.name} fallback={navFallback} />

      <header className="flex items-end gap-6 px-12 pt-2 max-md:gap-4 max-md:px-4">
        <div className="w-[132px] shrink-0 overflow-hidden rounded-xl bg-[var(--poster-placeholder)] shadow-[0_20px_48px_rgba(0,0,0,0.5)] ring-1 ring-white/[0.1] max-md:w-[92px]">
          <PosterImage
            src={person.avatarUrl}
            alt={person.name}
            className="aspect-[2/3] w-full object-cover"
            fallback={
              <div
                aria-hidden="true"
                className="grid aspect-[2/3] w-full place-items-center bg-gradient-to-b from-white/[0.07] to-white/[0.02] text-[34px] font-semibold text-white/30"
              >
                {person.name.trim()[0] ?? "?"}
              </div>
            }
          />
        </div>
        <div className="min-w-0 flex-1 pb-1">
          <p className="text-caption font-semibold uppercase tracking-[0.22em] text-[var(--accent-2)]">
            TMDB 影人
          </p>
          <h1 className="text-on-image mt-2 text-[32px] font-bold leading-[1.12] tracking-[-0.02em] text-white max-md:mt-1 max-md:text-[20px]">
            {person.name}
          </h1>
          <p className="tnum mt-3 text-ui text-white/70 max-md:mt-2 max-md:text-sub">
            共 {person.items.length} 部影视作品
          </p>
        </div>
      </header>

      <div className="mt-8 px-12 max-md:mt-6 max-md:px-4">
        {person.items.length > 0 ? (
          <CreditGrid items={person.items} isSubscribed={isSubscribed} onOpen={open} />
        ) : (
          <p className="rounded-xl border border-white/[0.06] bg-white/[0.025] px-5 py-8 text-center text-ui text-[var(--text-muted)]">
            TMDB 暂未收录这位影人的影视作品
          </p>
        )}
      </div>
    </div>
  );
}

/** 完整作品网格；状态只占用右上斜标，不额外挤压海报下方的年份信息。 */
function CreditGrid({
  items,
  isSubscribed,
  onOpen,
}: {
  items: MediaItem[];
  isSubscribed: (item: MediaItem) => boolean;
  onOpen: (item: MediaItem) => void;
}) {
  return (
    <section>
      <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
        全部作品
        <span className="tnum ml-2 text-sub font-normal text-[var(--text-faint)]">
          {items.length}
        </span>
      </h2>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(126px,1fr))] gap-4 max-md:grid-cols-3 max-md:gap-3">
        {items.map((item) => (
          <CreditCard
            key={`${item.type}:${item.id}`}
            item={item}
            subscribed={isSubscribed(item)}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  );
}

function CreditCard({
  item,
  subscribed,
  onOpen,
}: {
  item: MediaItem;
  subscribed: boolean;
  onOpen: (item: MediaItem) => void;
}) {
  // 「已入库」的绿斜标由 PosterCardVisual 依 libraryStatus 全站统一渲染，这里只补订阅态；
  // 两种状态同时存在时优先“已入库”：它表达作品已经可用，比追踪关系更直接。
  const subscribedOnly = !item.libraryStatus && subscribed;
  return (
    <PosterCardVisual
      item={
        subscribedOnly
          ? { ...item, ribbon: "已订阅", ribbonVariant: "compact-left", ribbonTone: "subscribed" }
          : item
      }
      action="none"
      onClick={() => onOpen(item)}
    />
  );
}

function PersonFallback({ failure }: { failure: "missing" | "error" }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
      <p className="text-body-lg font-semibold text-[var(--text)]">
        {failure === "missing" ? "TMDB 中没有这位影人" : "未能加载影人作品"}
      </p>
      <p className="max-w-sm text-ui leading-6 text-[var(--text-muted)]">
        {failure === "missing"
          ? "这条影人记录可能已被 TMDB 合并或移除。"
          : "请稍后重试；若持续失败，请检查 TMDB 网络连接。"}
      </p>
      <Link href={"/discover/movie" as Route} className="btn-glass px-4 py-2 text-ui font-medium">
        <ArrowLeftIcon className="size-4" />
        返回发现页
      </Link>
    </div>
  );
}
