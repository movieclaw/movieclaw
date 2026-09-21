"use client";

import Link from "next/link";
import type { Route } from "next";

import { HScroller } from "@/components/h-scroller";
import { PosterImage } from "@/components/poster-image";
import { useTapGuard } from "@/lib/use-tap-guard";

/**
 * 演职员横滚条 —— 媒体库条目详情页与发现页条目详情页共用。
 *
 * 两页的数据来源不同（那边是本地刮削 NFO / 库内档案，这边是 TMDB credits /
 * 豆瓣 actors），但呈现完全一致：一排 2:3 头像卡，卡下是姓名与饰演角色。
 *
 * —— 关于「没有头像的人」——
 * 缺头像不是加载失败，也不是入库丢了数据：TMDB 对配角、尤其是华语片的配角，
 * 大量条目本来就没有 profile_path，NFO 与库内档案都忠实地记着「这个人没有图」。
 * 而演员表是按剧组给的主次顺序排的，越往后越没图——直接留一个空灰块，一长排
 * 看过去就全是洞。所以这里给缺图的人渲染姓名首字：一格仍然是一位演员，
 * 只是没有照片，而不是一个坏掉的图片位。姓名与角色本身就是信息，
 * 不能因为缺图就把人整行丢掉。
 */
export interface CastRowPerson {
  name: string;
  /** 饰演角色；数据源未提供为空 */
  role?: string | null;
  /** 导演等非演员岗位；有值时直接展示，不添加「饰」前缀。 */
  credit?: string | null;
  /** 头像地址（调用方负责走图片代理）；数据源未提供为空 */
  avatarUrl?: string | null;
  /**
   * TMDB 影人 ID：有值时这一格链到调用方指定的人物页。姓名不是身份，
   * 没有 id 就不给链接——宁可不可点，也不能点开一个同名的另一个人。
   * 豆瓣来源、以及第三方刮削器写的老 NFO 都可能没有这个 id。
   */
  tmdbPersonId?: number | null;
}

type PersonHrefPrefix = "/people" | "/discover/people";

/**
 * 姓名 → 头像占位里显示的字。
 * 中日韩姓名取前一个字（「张国立」→「张」）；拉丁姓名取首尾单词的首字母
 * （「Tim Robbins」→「TR」）——两种写法各自最容易被认出来。
 */
function initialsOf(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  if (/[㐀-鿿぀-ヿ가-힯]/.test(trimmed)) return trimmed[0];
  const words = trimmed.split(/\s+/).filter(Boolean);
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export function CastRow({
  cast,
  title = "演职员",
  personHrefPrefix = "/people",
}: {
  cast: CastRowPerson[];
  title?: string;
  /** 媒体库默认进入库内人物页；发现详情显式改为 TMDB 全作品页。 */
  personHrefPrefix?: PersonHrefPrefix;
}) {
  if (cast.length === 0) return null;
  return (
    <section>
      <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
        {title}
      </h2>
      <HScroller className="-mx-1 gap-3 px-1 pb-1">
        {/* 同一个人可能出现多次（一人分饰两角；也可能既是导演又是演员），
            姓名+岗位不足以唯一，附加下标兜底。 */}
        {cast.map((person, index) => (
          <CastCard
            key={`${person.name}-${person.credit ?? person.role ?? ""}-${index}`}
            person={person}
            personHrefPrefix={personHrefPrefix}
          />
        ))}
      </HScroller>
    </section>
  );
}

/** 一格演员：有 TMDB 影人 ID 时整格是通往人物页的链接，否则是静态块。 */
function CastCard({
  person,
  personHrefPrefix,
}: {
  person: CastRowPerson;
  personHrefPrefix: PersonHrefPrefix;
}) {
  const subtitle = person.credit ?? (person.role ? `饰 ${person.role}` : null);
  // tapGuard：演员卡在横滚行里，滑动/刹车手势派发的 click 拦下不跳人物页
  // （Link 分支不传动作，放行的点击走默认导航——同 PosterCard 的接法）。
  // 无 tmdbPersonId 的静态块用不到，但 hook 必须无条件先调
  const tapGuard = useTapGuard();
  const body = (
    <>
      <div className="aspect-[2/3] overflow-hidden rounded-xl bg-[var(--poster-placeholder)] ring-1 ring-white/[0.08] transition-all duration-300 ease-out group-hover/cast:-translate-y-1 group-hover/cast:shadow-[0_16px_38px_rgba(0,0,0,0.5)] group-hover/cast:ring-white/25">
        <PosterImage
          src={person.avatarUrl ?? ""}
          alt={person.name}
          className="size-full object-cover"
          fallback={
            <div
              aria-hidden="true"
              className="grid size-full place-items-center bg-gradient-to-b from-white/[0.07] to-white/[0.02] text-[26px] font-semibold tracking-wide text-white/30"
            >
              {initialsOf(person.name)}
            </div>
          }
        />
      </div>
      <p className="mt-1.5 truncate text-sub font-medium text-[var(--text)]">{person.name}</p>
      {subtitle && (
        <p className="truncate text-caption text-[var(--text-faint)]">{subtitle}</p>
      )}
    </>
  );

  if (!person.tmdbPersonId) {
    return <div className="w-[104px] shrink-0">{body}</div>;
  }
  return (
    <Link
      href={`${personHrefPrefix}/${person.tmdbPersonId}` as Route}
      {...tapGuard}
      aria-label={`查看 ${person.name} 的影人页`}
      className="group/cast w-[104px] shrink-0 rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
    >
      {body}
    </Link>
  );
}
