"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { Route } from "next";

import { BrandLoader } from "@/components/brand-loader";
import { ChevronLeftIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { fetchPerson, type PersonCredit, type PersonDetail } from "@/lib/api/people";
import { HttpError } from "@/lib/http";
import { useBackNavigation } from "@/lib/back-navigation";
import { useResolvedTheme } from "@/themes/registry";
import { usePageTitle } from "@/lib/use-page-title";

/**
 * 人物页：**我库里**的这个影人。
 *
 * 与两个影片详情页的分工：那两页回答「这部片是什么」，这页回答「这个人是谁、
 * 他在我库里有哪些片」。数据全部来自本地表（person / media_item_person），
 * 不联网——因此这里没有生平简介、出生地这类需要实时拉取的字段，那属于
 * 「这个人本身是什么」，与本页职责不同。
 *
 * 版式刻意比影片详情页素：这页的主体是作品海报墙，人物本身只需要头像 + 姓名
 * 一行交代清楚。不铺沉浸背景——影人没有「专属剧照」，硬拿某部片的剧照当背景
 * 会误导（看着像在讲那部片）。
 */
export function PersonDetailView({ tmdbPersonId }: { tmdbPersonId: number | string }) {
  const [person, setPerson] = useState<PersonDetail | null>(null);
  const navFallback = { label: "媒体库", href: "/library" as Route };
  // Netflix 桌面返回语言一套：全出血(isHome)详情页用 NetflixBackButton；带
  // PageNav 工具条的页面用 PageNav 内返回键；两者同图标 / 同尺寸档 / 同 4vw
  // 基线 / 同 useBackNavigation 行为。本页 Netflix 桌面换 NetflixBackButton
  // 浮在顶栏下（与条目详情页同一套）；移动端保留 PageNav——它要向外壳登记顶栏
  const back = useBackNavigation(navFallback.href);
  const { slots } = useResolvedTheme();
  const DetailNav = slots.detailNav;
  // null=加载中；"missing"=库内没有这个人；"error"=其他失败
  const [failure, setFailure] = useState<"missing" | "error" | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPerson(null);
    setFailure(null);
    fetchPerson(tmdbPersonId)
      .then((data) => {
        if (!cancelled) setPerson(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 404 = 库内没有这个人：多半是存量库还没做过一次「刷新元数据」，
        // 这不是错误而是「暂无数据」，文案要分开说，否则用户会以为坏了
        setFailure(err instanceof HttpError && err.status === 404 ? "missing" : "error");
      });
    return () => {
      cancelled = true;
    };
  }, [tmdbPersonId]);

  usePageTitle(person?.name);

  // 兜底态也要渲染 PageNav（姓名未知，标签留空）：向外壳登记「本页自带顶栏」，
  // 否则移动端全局顶栏（☰ + logo）会先显示再消失、顶部闪一下。
  if (failure !== null) {
    return (
      <div className="flex h-full flex-col">
        <DetailNav title="" fallback={navFallback} onBack={back} />
        <PersonFallback failure={failure} />
      </div>
    );
  }
  if (person === null) {
    return (
      <div className="flex h-full flex-col">
        <DetailNav title="" fallback={navFallback} onBack={back} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <BrandLoader className="size-5" />
          正在读取影人档案…
        </div>
      </div>
    );
  }

  const cast = person.credits.filter((c) => c.department === "cast");
  const directed = person.credits.filter((c) => c.department === "director");

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto pb-12">
      {/* 来路交给统一后退：进人物页的入口是各处演职员条，来路不固定；
          只有分享链接或新标签直达时才兜底去媒体库。
          不补负边距——本页的横向内边距在各分区上，滚动容器自身没有 px。 */}
      {/* onPhoto：NetflixBackButton 悬在左上角、头像卡已让位到键底之下
          （globals.css 的 .person-hero 桌面档），实底深灰圆盘作为亮图兜底
          保留；加载/失败态无头像卡，行为一致无妨 */}
      <DetailNav title={person.name} fallback={navFallback} onBack={back} onPhoto />

      {/* 头部：头像 + 姓名。刻意不做大 Hero——影人没有专属剧照。
          person-hero：Netflix 桌面让位钩子（globals.css 桌面档把头部推到
          NetflixBackButton 键底之下，银玻璃与移动端不吃这条规则） */}
      <header className={`person-hero flex items-end gap-6 pt-2 content-inset max-md:gap-4`}>
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
            影人
          </p>
          <h1 className="text-on-image mt-2 text-[32px] font-bold leading-[1.12] tracking-[-0.02em] text-white max-md:mt-1 max-md:text-[20px]">
            {person.name}
          </h1>
          {person.originalName && person.originalName !== person.name && (
            <p className="text-on-image mt-1 truncate text-ui text-white/55 max-md:text-sub">
              {person.originalName}
            </p>
          )}
          <p className="tnum mt-3 text-ui text-white/70 max-md:mt-2 max-md:text-sub">
            库内 {person.credits.length} 部
            {cast.length > 0 && directed.length > 0
              ? `（参演 ${cast.length} · 执导 ${directed.length}）`
              : ""}
          </p>
        </div>
      </header>

      <div className={`mt-8 space-y-8 max-md:mt-6 max-md:space-y-6 content-inset`}>
        <CreditGrid title="参演" credits={cast} showCharacter />
        <CreditGrid title="执导" credits={directed} />
      </div>
    </div>
  );
}

/**
 * 作品网格：一格一部库内影片，点进条目详情页。
 *
 * 用网格而不是横滚条：一个演员在库里可能有几十部，横滚条要一直划；
 * 而这页的主体就是这批作品，值得占满宽度铺开。
 */
function CreditGrid({
  title,
  credits,
  showCharacter = false,
}: {
  title: string;
  credits: PersonCredit[];
  showCharacter?: boolean;
}) {
  if (credits.length === 0) return null;
  return (
    <section>
      <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
        {title}
        <span className="tnum ml-2 text-sub font-normal text-[var(--text-faint)]">
          {credits.length}
        </span>
      </h2>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(126px,1fr))] gap-4 max-md:grid-cols-3 max-md:gap-3">
        {credits.map((credit) => (
          <CreditCard key={`${credit.mediaItemId}-${credit.department}`} credit={credit} showCharacter={showCharacter} />
        ))}
      </div>
    </section>
  );
}

function CreditCard({ credit, showCharacter }: { credit: PersonCredit; showCharacter: boolean }) {
  const body = (
    <>
      <div className="aspect-[2/3] overflow-hidden rounded-xl bg-[var(--poster-placeholder)] ring-1 ring-white/[0.08] transition-all duration-300 ease-out group-hover/credit:-translate-y-1 group-hover/credit:shadow-[0_18px_44px_rgba(0,0,0,0.55)] group-hover/credit:ring-white/25">
        <PosterImage
          src={credit.posterUrl}
          alt={`${credit.title} 海报`}
          className="size-full object-cover"
        />
      </div>
      <p className="text-on-image mt-1.5 truncate text-sub font-medium text-[var(--text)]">
        {credit.title}
      </p>
      <p className="text-on-image tnum truncate text-caption text-[var(--text-faint)]">
        {showCharacter && credit.character ? `饰 ${credit.character}` : credit.year || ""}
      </p>
    </>
  );

  // 文件全删、只剩档案的条目没有可跳转的库，渲染成不可点（而不是给一个 404 链接）
  if (credit.libraryId === undefined) {
    return <div className="group/credit block">{body}</div>;
  }
  return (
    <Link
      href={`/library/${credit.libraryId}/item/${credit.mediaItemId}` as Route}
      aria-label={`查看《${credit.title}》`}
      className="group/credit block rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
    >
      {body}
    </Link>
  );
}

/**
 * 空状态。「库内没有他的作品」与「加载失败」必须分开说：前者不是错误，
 * 把它说成错误会让用户去查网络、白费功夫。它有两个成因，文案要都盖住：
 *   1. 存量库还没做过一次「刷新元数据」（老档案里没有 TMDB 影人 ID，
 *      见后端迁移 c4d7a9e2b581）；
 *   2. 他参演的片都已从库里删掉（影人行只增不删，会留下孤儿）。
 */
function PersonFallback({ failure }: { failure: "missing" | "error" }) {
  return (
    // flex-1 而非 h-full：调用处在其上方叠了一条 PageNav（flex 纵向布局）
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
      {failure === "missing" ? (
        <>
          <p className="text-body-lg font-semibold text-[var(--text)]">库内没有这位影人的作品</p>
          <p className="max-w-md text-ui leading-6 text-[var(--text-muted)]">
            可能是他参演的片都已从库里移除；也可能这个库是早前扫描的——影人档案随入库刮削
            一并建立，对它执行一次「刷新元数据」即可补齐，之后这里就会列出他在库内的全部作品。
          </p>
          <Link href={"/library" as Route} className="btn-glass px-4 py-2 text-ui font-medium">
            <ChevronLeftIcon className="size-4" />
            去媒体库
          </Link>
        </>
      ) : (
        <>
          <p className="text-body-lg font-semibold text-[var(--text)]">未能加载影人档案</p>
          <p className="max-w-sm text-ui leading-6 text-[var(--text-muted)]">
            请稍后重试；若持续失败，请查看系统日志。
          </p>
        </>
      )}
    </div>
  );
}
