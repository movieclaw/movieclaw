"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ArtworkPickerDialog } from "@/components/artwork-picker-dialog";
import { CastRow } from "@/components/cast-row";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { HScroller } from "@/components/h-scroller";
import {
  ArrowLeftIcon,
  FolderIcon,
  MoreIcon,
  StarIcon,
  TrashIcon,
} from "@/components/icons";
import { useConfirm } from "@/components/feedback";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import { Tooltip } from "@/components/tooltip";
import {
  type AudioStream,
  type ItemDeleteResult,
  type LibraryEpisode,
  type LibraryItemDetail,
  type LibraryItemFile,
  type MediaLibrary,
  type ReidentifyResult,
  type SeasonEpisodes,
  type SubtitleStream,
  type TransferPreview,
  type TransferStatus,
  deleteLibraryFile,
  deleteLibraryItem,
  getItemEpisodes,
  getLibrary,
  getLibraryItemDetail,
  getTransferStatus,
  listLibraries,
  previewItemTransfer,
  refreshItemMetadata,
  reidentifyLibraryItem,
  transferLibraryItem,
} from "@/lib/api/libraries";
import { useBackdrop } from "@/lib/backdrop";
import { formatBytes } from "@/lib/format";
import { resolveRequestUrl } from "@/lib/http";
import { cachedImageUrl } from "@/lib/image-proxy";
import { refreshItemConfirm } from "@/lib/library-confirm";
import { formatRelativeTime } from "@/lib/time";
import { usePageTitle } from "@/lib/use-page-title";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/**
 * 媒体库条目详情页（/library/[id]/item/[mediaItemId]）——与发现页详情
 * （MediaDetailView，纯 TMDB 实时数据）职责不同：这里回答的是
 * 「**我拥有的这份拷贝**是什么」，全部信息来自本地刮削成果与文件本体：
 *
 *   1. Hero 背景优先条目目录里的 fanart（本地美术图接口），其次 TMDB 剧照；
 *   2. 简介 / 评分 / 片长 / 演职员来自条目目录的 NFO（TMM/Emby 刮削产物）；
 *   3. 片源规格来自 ffprobe 对文件本体的探测（分辨率/编码/HDR/音轨/字幕），
 *      电影多版本（1080p 与 2160p 并存）用版本切换器合并展示；
 *   4. 底部文件区列出原始文件名 + 尺寸，悬浮看物理路径——识别错了
 *      用户要能立刻知道"这是哪个文件"；
 *   5. 条目级操作：重新识别（识别器升级后的翻案通道）与删除（唯一会
 *      真删磁盘的入口，整个刮削目录一起清，二次确认）。文件行与分集
 *      文件卡上另有单文件删除（多版本洗掉一个 / 删某集重下，同样二次
 *      确认；最后一个文件升级为整条目删除）。
 */
export function LibraryItemDetailView({
  libraryId,
  mediaItemId,
}: {
  libraryId: number;
  mediaItemId: number;
}) {
  const router = useRouter();
  const confirm = useConfirm();
  const [detail, setDetail] = useState<LibraryItemDetail | null>(null);
  const [library, setLibrary] = useState<MediaLibrary | null>(null);
  const [failed, setFailed] = useState(false);
  // 重新识别：进行中 / 结论横幅（含"查看新条目"等后续动作）
  const [reidentifying, setReidentifying] = useState(false);
  const [reidentifyResult, setReidentifyResult] = useState<ReidentifyResult | null>(null);
  const [reidentifyError, setReidentifyError] = useState<string | null>(null);
  // 元数据刷新：**进行中的状态以服务端 detail.scraping 为准**（离开页面、
  // 刷新浏览器、换设备打开都能接着看到）；这个本地态只覆盖"点击到接口
  // 返回"这一小段，避免按钮闪一下没反应
  const [kicking, setKicking] = useState(false);
  // 「更换图片」弹层（手动选海报/背景，选后加锁）
  const [artworkOpen, setArtworkOpen] = useState(false);
  // 删除确认弹窗
  const [deleteOpen, setDeleteOpen] = useState(false);
  // 单文件删除确认弹窗（非 null 即打开；多版本洗版 / 删某集重下的入口）
  const [deleteFileTarget, setDeleteFileTarget] = useState<LibraryItemFile | null>(null);
  // 转移到其他库的弹窗（选库 → 预览 → 执行 → 进度 → 结论）
  const [transferOpen, setTransferOpen] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    Promise.all([
      getLibraryItemDetail(libraryId, mediaItemId),
      getLibrary(libraryId).catch(() => null),
    ])
      .then(([data, lib]) => {
        setDetail(data);
        setLibrary(lib);
      })
      .catch(() => setFailed(true));
  }, [libraryId, mediaItemId]);

  useEffect(() => {
    setDetail(null);
    setReidentifyResult(null);
    setReidentifyError(null);
    probeRetries.current = 0; // 换条目重置静默补拉的重试预算
    reload();
  }, [reload]);

  usePageTitle(detail?.title);

  // 沉浸模式：进入详情页把全站背景（body 大图 + 玻璃折射纹理）临时换成
  // 该片剧照，玻璃 UI 整体染上影片氛围；离开页面自动恢复用户配置的背景。
  // 依赖 URL 字符串而非 detail 对象：规格补拉刷新 detail 时背景不重挂
  const { setOverrideBackdrop } = useBackdrop();
  const immersiveUrl = detail ? imageUrl(detail.backdrop_url ?? detail.poster_url) : "";
  useEffect(() => {
    if (!immersiveUrl) return;
    setOverrideBackdrop(immersiveUrl);
    return () => setOverrideBackdrop(null);
  }, [immersiveUrl, setOverrideBackdrop]);

  // 刮削进行中就轮询到它结束——状态在服务端（detail.scraping），所以
  // **无论刷新是从这个页面发起的、还是别处发起后你才打开这一页**，都会
  // 看到"正在刷新"并在结束时自动呈现新档案/新图；离开页面也不影响后台跑完
  const scrapingNow = Boolean(detail?.scraping) || kicking;
  useVisiblePolling(
    () => {
      getLibraryItemDetail(libraryId, mediaItemId)
        .then(setDetail)
        .catch(() => {});
    },
    detail?.scraping ? 2000 : null,
  );

  // 介质规格的静默补拉：后端把 ffprobe 补探挪到了响应之后的后台任务
  // （网络挂载上探测是秒级/文件，不能拖首屏），"尚未探测"的文件在页面
  // 打开后每 3 秒补拉一次详情呈现结果，最多三次（ffprobe 缺失时不空转）
  const probeRetries = useRef(0);
  useEffect(() => {
    if (!detail) return;
    const pending = detail.files.some((f) => !f.missing && f.audio_streams === null);
    if (!pending || probeRetries.current >= 3) return;
    const timer = setTimeout(() => {
      probeRetries.current += 1;
      getLibraryItemDetail(libraryId, mediaItemId)
        .then(setDetail)
        .catch(() => {});
    }, 3000);
    return () => clearTimeout(timer);
  }, [detail, libraryId, mediaItemId]);

  // 兜底态（加载中/失败）的顶栏：条目标题未知，末项留空——渲染 PageNav 是为了
  // 向外壳登记「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失，
  // 顶部闪一下；同时转圈期间就有返回键可点。祖先链路与正文的 trail 保持一致。
  const fallbackTrail = [
    { label: "媒体库", href: "/library" as Route },
    { label: library?.name ?? "库存", href: `/library/${libraryId}` as Route },
    { label: "" },
  ];

  if (failed) {
    return (
      // ambient-fallback：同 MediaDetailView——本页豁免全局蒙版，兜底态没有沉浸
      // 背景可铺，文案会压在用户壁纸上，自己带一层底才读得清
      <div className="ambient-fallback flex h-full flex-col">
        <PageNav items={fallbackTrail} />
        <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
          <p className="text-body-lg font-semibold text-[var(--text)]">未能加载该条目</p>
          <p className="max-w-sm text-ui leading-6 text-[var(--text-muted)]">
            条目可能已被删除或重新识别为其他作品，请回库存页查看。
          </p>
          <Link
            href={`/library/${libraryId}` as Route}
            className="btn-glass flex items-center gap-2 px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            <ArrowLeftIcon className="size-4" />
            回到库存页
          </Link>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="ambient-fallback flex h-full flex-col">
        <PageNav items={fallbackTrail} />
        <div className="flex flex-1 items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在读取本地刮削信息…
        </div>
      </div>
    );
  }

  const isMovie = detail.kind === "movie";
  const meta = detail.local_meta;
  const posterUrl = imageUrl(detail.poster_url);
  // 片长：NFO 的 runtime 优先，其次任意文件的实测时长
  const runtimeMinutes =
    meta?.runtime_minutes ??
    (() => {
      const probed = detail.files.find((f) => f.duration_seconds)?.duration_seconds;
      return probed ? Math.round(probed / 60) : null;
    })();

  const trail = [
    { label: "媒体库", href: "/library" as Route },
    { label: library?.name ?? "库存", href: `/library/${libraryId}` as Route },
    { label: detail.title },
  ];

  const runReidentify = async () => {
    setReidentifying(true);
    setReidentifyError(null);
    setReidentifyResult(null);
    try {
      setReidentifyResult(await reidentifyLibraryItem(libraryId, mediaItemId));
    } catch (err) {
      setReidentifyError(err instanceof Error ? err.message : "重新识别失败，请稍后重试");
    } finally {
      setReidentifying(false);
    }
  };

  const runMetadataRefresh = async () => {
    // 重操作先确认：单条目刷新是 force 语义（图片覆盖重下），说清再动手
    if (!(await confirm(refreshItemConfirm(detail?.title ?? "此条目")))) return;
    setKicking(true);
    try {
      await refreshItemMetadata(libraryId, mediaItemId);
      // 立刻重拉一次详情：服务端的 scraping 标志会接管后续状态展示，
      // 前端不再盲等固定秒数（那种写法一旦离开页面状态就丢了）
      reload();
      // 后台任务在响应发出后才起跑，上面那次 reload 可能抢在 scraping
      // 标志立起之前拉到 false——稍后再拉一次兜底，否则界面毫无动静、
      // 轮询也不会启动，用户会以为没点上
      setTimeout(reload, 1500);
    } catch (err) {
      setReidentifyError(err instanceof Error ? err.message : "元数据刷新失败，请稍后重试");
    } finally {
      setKicking(false);
    }
  };

  return (
    // rounded-2xl + overflow 裁切：内容渐变到底部是近实色的深色板，方角
    // 会与全站"浮起圆角卡片"的形状语言冲突——按侧栏同规格圆角收尾。
    // max-md:rounded-none：这套圆角只在桌面成立——桌面外壳有 p-3.5 的留白，
    // 圆角落在留白里、背景大图从四周透出，才是一张"浮起的卡片"。窄屏是通栏
    // 满屏布局，没有留白，圆角直接压在屏幕边上：顶栏的吸顶雾层被这层
    // overflow 一裁，就成了贴在屏幕顶上的一块圆角色块（手机上肉眼可见的
    // 两个缺角），底边同理被 Home 指示条切掉。手机上一律方角、真通栏。
    <div className="scroll-thin scroll-safe h-full overflow-y-auto rounded-2xl max-md:rounded-none">
      {/* —— 顶部：不再有任何 hero 卡片图层——全站背景此刻就是本片剧照
          （沉浸覆盖 + 本页豁免全局蒙版，见 app-shell 的 isHome），大图
          直出、零边界。顶栏首屏只有一颗圆形返回键浮在剧照上（吸顶蒙版
          此时全透明），页面顶部不再有大面积色块 —— */}
      <PageNav
        items={trail}
        actions={
          <ItemActionsMenu
            reidentifying={reidentifying}
            scraping={scrapingNow}
            searchHref={`/search?q=${encodeURIComponent(detail.title)}` as Route}
            onReidentify={runReidentify}
            onRefreshMetadata={runMetadataRefresh}
            onTransfer={() => setTransferOpen(true)}
            onDelete={() => setDeleteOpen(true)}
          />
        }
      />

      {/* 氛围留白：这一段什么都不放，让剧照完整呼吸 */}
      <div className="h-[30vh] min-h-[180px] max-md:h-[22vh] max-md:min-h-[120px]" />

      {/* —— 内容层：从全透明渐入页面底色，与背景之间没有任何接缝 —— */}
      <div className="bg-[linear-gradient(180deg,rgba(7,9,14,0)_0,rgba(7,9,14,0.66)_130px,rgba(7,9,14,0.88)_360px,rgba(7,9,14,0.93)_100%)] pb-12">
      {/* —— 头部信息区 —— */}
      <div className="relative z-10 flex items-end gap-7 px-12 pt-6 max-md:gap-4 max-md:px-4 max-md:pt-3">
        {/* 海报：悬浮浮出「更换图片」——选图入口放在它作用的对象上
            （Emby/Plex 同款位置），不必再进顶栏的 ⋯ 菜单绕一圈 */}
        <div className="group/poster relative w-[186px] shrink-0 overflow-hidden rounded-xl bg-[#141824] shadow-[0_26px_60px_rgba(0,0,0,0.6)] ring-1 ring-white/15 max-md:w-[104px]">
          <PosterImage
            src={posterUrl}
            alt={`${detail.title} 海报`}
            className="aspect-[2/3] w-full object-cover"
          />
          <button
            type="button"
            onClick={() => setArtworkOpen(true)}
            className="touch-reveal absolute inset-x-0 bottom-0 flex h-11 items-center justify-center bg-black/70 text-sub font-medium text-white opacity-0 backdrop-blur-sm transition group-hover/poster:opacity-100 max-md:h-8 max-md:text-caption"
          >
            更换图片
          </button>
        </div>

        <div className="min-w-0 flex-1 pb-1">
          <p className="text-caption font-semibold uppercase tracking-[0.22em] text-[var(--accent-2)]">
            已入库 · {isMovie ? "电影" : "剧集"}
            {meta && meta.genres.length > 0 ? ` · ${meta.genres.join(" / ")}` : ""}
          </p>
          <h1 className="text-on-image mt-2 text-[38px] font-bold leading-[1.1] tracking-[-0.02em] text-white max-md:mt-1 max-md:text-[21px]">
            {detail.title}
          </h1>
          {detail.original_title && detail.original_title !== detail.title && (
            <p className="text-on-image mt-1.5 truncate text-body text-white/55">
              {detail.original_title}
            </p>
          )}

          <div className="tnum mt-3.5 flex flex-wrap items-center gap-x-3.5 gap-y-2 text-ui text-white/80 max-md:mt-2 max-md:gap-x-2.5 max-md:gap-y-1 max-md:text-sub">
            {meta?.rating != null && (
              <span className="flex items-center gap-1.5">
                <StarIcon className="size-4 text-[#f5c451]" />
                <span className="text-title-sm font-bold text-white">{meta.rating.toFixed(1)}</span>
              </span>
            )}
            {detail.year && <span>{detail.year}</span>}
            {runtimeMinutes != null && <span>{runtimeMinutes} 分钟</span>}
            {meta && meta.directors.length > 0 && (
              <span className="text-white/65">导演 {meta.directors.join(" / ")}</span>
            )}
            {detail.total_size_bytes > 0 && <span>{formatBytes(detail.total_size_bytes)}</span>}
            <QualityBadges files={detail.files} />
          </div>

          {/* 外部信息源：识别结果对应的站点词条，用于人工核对刮削是否配错片。
              刻意压到元信息行下方的小字，不与主操作争视觉权重 */}
          <div className="mt-3 flex flex-wrap items-center gap-x-3.5 gap-y-1">
            {detail.tmdb_id > 0 && (
              <SourceLink
                href={`https://www.themoviedb.org/${detail.kind}/${detail.tmdb_id}`}
                label="TMDB"
              />
            )}
            {detail.imdb_id && (
              <SourceLink
                href={`https://www.imdb.com/title/${detail.imdb_id}/`}
                label="IMDb"
              />
            )}
          </div>

          {/* 刮削进行中的状态条：与库页的整库刷新面板同一套语言（阶段文案
              也同源），状态在服务端——离开页面/刷新浏览器回来照样看得到 */}
          {scrapingNow && (
            <div className="mt-4 flex max-w-2xl items-center gap-2 rounded-xl border border-[#7dd3fc]/25 bg-[#7dd3fc]/[0.07] px-4 py-2.5 text-sub font-medium text-[#7dd3fc]">
              <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-[#7dd3fc]/30 border-t-[#7dd3fc]" />
              <span className="truncate">
                正在刷新元数据
                {detail.scraping_phase ? ` · ${detail.scraping_phase}` : ""}
              </span>
              <span className="ml-auto shrink-0 text-caption font-normal text-white/45">
                完成后自动更新本页
              </span>
            </div>
          )}

          {/* 重新识别的结论横幅：结果与后续动作当场给出 */}
          {(reidentifyResult || reidentifyError) && (
            <div className="mt-4 max-w-2xl rounded-xl border border-white/[0.1] bg-[rgba(14,16,22,0.6)] px-4 py-3 text-sub leading-6 text-white/85 backdrop-blur-md">
              {reidentifyError ? (
                <span className="text-[#ff9f9f]">{reidentifyError}</span>
              ) : (
                <>
                  <span>{reidentifyResult!.message}</span>
                  <span className="ml-3 inline-flex gap-3">
                    {reidentifyResult!.changed && reidentifyResult!.new_media_item_id != null && (
                      <button
                        type="button"
                        onClick={() =>
                          router.replace(
                            `/library/${libraryId}/item/${reidentifyResult!.new_media_item_id}` as Route,
                          )
                        }
                        className="font-medium text-[var(--accent-2)] hover:underline"
                      >
                        查看新条目 →
                      </button>
                    )}
                    {reidentifyResult!.unidentified > 0 && (
                      <Link
                        href={`/library/${libraryId}` as Route}
                        className="font-medium text-[var(--accent-2)] hover:underline"
                      >
                        去待识别清单 →
                      </Link>
                    )}
                  </span>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="mt-9 space-y-8 px-12 max-md:mt-6 max-md:space-y-6 max-md:px-4">
        {/* —— 片源规格：电影多版本合并切换（剧集走下方分集区逐集展示）—— */}
        {isMovie && detail.files.length > 0 && <MovieVersionSpecs files={detail.files} />}

        {/* —— 剧情简介（本地 NFO 优先，TMDB 兜底）—— */}
        {meta?.plot && (
          <section>
            <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
              剧情简介
              <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                {meta.source === "nfo"
                  ? `信息来自本地刮削（${meta.nfo_name}）`
                  : meta.source === "db"
                    ? "信息来自本地档案"
                    : "信息来自 TMDB"}
              </span>
            </h2>
            <p className="text-on-image max-w-3xl text-body leading-7 text-white/78">
              {meta.plot}
            </p>
          </section>
        )}

        {/* —— 剧集分集区：季选择 + 分集横滚卡 + 选中集的简介/规格/文件 —— */}
        {!isMovie && detail.seasons.length > 0 && (
          <SeasonEpisodesSection
            libraryId={libraryId}
            detail={detail}
            onDeleteFile={setDeleteFileTarget}
          />
        )}

        {/* —— 演职员：与发现页条目详情共用同一个组件。头像来自 NFO 的 <thumb>，
            NFO 没写的按姓名回填库内档案的 profile_path（见后端 _fill_actor_thumbs）；
            TMDB 本就没有照片的人渲染姓名首字占位 —— */}
        {meta && (
          <CastRow
            cast={meta.actors.map((a) => ({
              name: a.name,
              role: a.role,
              avatarUrl: a.thumb_url ? cachedImageUrl(a.thumb_url) : null,
              tmdbPersonId: a.tmdb_person_id,
            }))}
          />
        )}

        {/* —— 文件区：电影列全部文件；剧集只列没归到集的零散文件 —— */}
        {(() => {
          const files = isMovie
            ? detail.files
            : detail.files.filter((f) => f.episode_number === 0);
          if (files.length === 0) return null;
          return (
            <FileSection
              files={files}
              isMovie={isMovie}
              title={isMovie ? "文件" : "其他文件"}
              onDeleteFile={setDeleteFileTarget}
            />
          );
        })()}
      </div>
      </div>

      <DeleteDialog
        open={deleteOpen}
        detail={detail}
        onClose={() => setDeleteOpen(false)}
        onDeleted={() => router.replace(`/library/${libraryId}` as Route)}
        libraryId={libraryId}
      />

      <DeleteFileDialog
        file={deleteFileTarget}
        detail={detail}
        libraryId={libraryId}
        onClose={() => setDeleteFileTarget(null)}
        onDeleted={(deletedItem) => {
          setDeleteFileTarget(null);
          if (deletedItem) router.replace(`/library/${libraryId}` as Route);
          else reload();
        }}
      />

      <TransferDialog
        open={transferOpen}
        detail={detail}
        libraryId={libraryId}
        sourceLibraryName={library?.name ?? null}
        onClose={() => setTransferOpen(false)}
        onTransferred={(targetLibraryId) =>
          router.replace(`/library/${targetLibraryId}/item/${mediaItemId}` as Route)
        }
      />

      <ArtworkPickerDialog
        open={artworkOpen}
        libraryId={libraryId}
        mediaItemId={mediaItemId}
        onClose={() => setArtworkOpen(false)}
        onChanged={reload}
      />
    </div>
  );
}

/**
 * 条目操作 ⋯ 菜单：重新识别 / 刷新元数据 / 转移到其他库 / 删除影片。
 *
 * 这几个都是低频且不可逆（改身份锚、重下全套图、搬目录、删文件）的操作，
 * 摆成常驻大按钮既压着正文，又把「误点」的成本摊在最显眼的位置。收进顶栏
 * 右上角的 ⋯ 后，页面主区只剩内容；跑起来之后的状态仍由正文里的进度条
 * 完整交代（与媒体库页「操作进 ⋯、状态看正文」的分工一致）。
 *
 * 「转移到其他库」与「重新识别」是**两种不同的错**的补救，菜单里紧挨着摆：
 * 前者是"片子认对了、库放错了"（韩剧进了大陆剧库），后者是"片子认错了"。
 */
function ItemActionsMenu({
  reidentifying,
  scraping,
  searchHref,
  onReidentify,
  onRefreshMetadata,
  onTransfer,
  onDelete,
}: {
  reidentifying: boolean;
  scraping: boolean;
  /** 站点资源搜索直达（预填片名）：手动补版本/换版本的入口 */
  searchHref: Route;
  onReidentify: () => void;
  onRefreshMetadata: () => void;
  onTransfer: () => void;
  onDelete: () => void;
}) {
  const router = useRouter();
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-ui font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const running = reidentifying || scraping;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          className={`${PAGE_NAV_BUTTON_CLASS} relative data-[state=open]:bg-black/55 data-[state=open]:text-white`}
        >
          <MoreIcon className="size-[18px] max-md:size-[22px]" />
          {/* 任务在跑时给触发键点一个小点：菜单收起后也知道后台还在忙 */}
          {running && (
            <span className="absolute right-1 top-1 size-1.5 animate-pulse rounded-full bg-[#7dd3fc]" />
          )}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[11rem] !rounded-xl p-1"
        >
          <DropdownMenu.Item
            onSelect={() => router.push(searchHref)}
            className={itemClass}
          >
            搜索资源
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          <DropdownMenu.Item
            onSelect={onReidentify}
            disabled={reidentifying}
            className={itemClass}
          >
            {reidentifying ? "正在重新识别…" : "重新识别"}
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onRefreshMetadata}
            disabled={scraping}
            className={itemClass}
          >
            {scraping ? "正在刷新元数据…" : "刷新元数据"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onTransfer} className={itemClass}>
            转移到其他库…
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-white/[0.07]" />
          <DropdownMenu.Item
            onSelect={onDelete}
            className={`${itemClass} !text-[#ff9f9f] data-[highlighted]:!bg-[rgba(255,90,90,0.16)] data-[highlighted]:!text-[#ffb4b4]`}
          >
            删除影片
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/* ------------------------------------------------------------------------ */
/* 展示格式化：ffprobe 原始值 → 用户认知的规格语言                              */
/* ------------------------------------------------------------------------ */

/** 图片地址：本地美术图是 API 相对路径（补 base），TMDB 图床走缓存代理。 */
function imageUrl(url: string | null): string {
  if (!url) return "";
  return /^https?:\/\//i.test(url) ? cachedImageUrl(url) : resolveRequestUrl(url);
}

const AUDIO_CODEC_LABELS: Record<string, string> = {
  aac: "AAC",
  ac3: "Dolby Digital",
  eac3: "Dolby Digital+",
  truehd: "Dolby TrueHD",
  dts: "DTS",
  flac: "FLAC",
  opus: "Opus",
  mp3: "MP3",
  vorbis: "Vorbis",
};

const SUBTITLE_CODEC_LABELS: Record<string, string> = {
  subrip: "SRT",
  srt: "SRT",
  ass: "ASS",
  ssa: "SSA",
  hdmv_pgs_subtitle: "PGS",
  dvd_subtitle: "VobSub",
  mov_text: "Text",
  webvtt: "VTT",
  vtt: "VTT",
  sub: "VobSub",
  sup: "PGS",
};

const VIDEO_CODEC_LABELS: Record<string, string> = {
  hevc: "HEVC",
  h264: "H.264",
  h265: "HEVC",
  av1: "AV1",
  vc1: "VC-1",
  mpeg2video: "MPEG-2",
  vp9: "VP9",
};

const LANGUAGE_LABELS: Record<string, string> = {
  chi: "中文",
  zho: "中文",
  cmn: "中文",
  yue: "粤语",
  eng: "英语",
  jpn: "日语",
  kor: "韩语",
  fre: "法语",
  fra: "法语",
  ger: "德语",
  deu: "德语",
  spa: "西班牙语",
  rus: "俄语",
  ita: "意大利语",
  por: "葡萄牙语",
  tha: "泰语",
  hin: "印地语",
};

function languageLabel(code: string | null): string | null {
  if (!code || code === "und") return null;
  return LANGUAGE_LABELS[code.toLowerCase()] ?? code;
}

/** 声道数 → 惯用布局标签（channel_layout 可用时优先，去掉 (side) 等后缀）。 */
function channelsLabel(stream: AudioStream): string | null {
  const layout = stream.channel_layout?.split("(")[0]?.trim();
  if (layout && /^\d/.test(layout)) return layout;
  const map: Record<number, string> = { 1: "单声道", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1" };
  if (stream.channels == null) return null;
  return map[stream.channels] ?? `${stream.channels} 声道`;
}

// 这些 profile 值只是编码内部档次（如 AAC 的 LC、HEVC 的 Main 10），单独展示
// 反而让人困惑——只有 DTS-HD MA 这类"比 codec 更有信息量"的 profile 才顶替 codec
const GENERIC_PROFILES = new Set(["lc", "main", "high", "baseline", "main 10"]);

/** 音轨 → 一行徽章文案：格式（有信息量的 profile 优先）· 声道 · 语言。 */
function audioLabel(stream: AudioStream): string {
  const profile =
    stream.profile && !GENERIC_PROFILES.has(stream.profile.toLowerCase()) ? stream.profile : null;
  const codec =
    profile ||
    (stream.codec ? (AUDIO_CODEC_LABELS[stream.codec] ?? stream.codec.toUpperCase()) : "未知格式");
  return [codec, channelsLabel(stream), languageLabel(stream.language)]
    .filter(Boolean)
    .join(" · ");
}

/** 字幕 → 一行徽章文案：语言/标题 · 格式 (+ 强制/外挂标记在徽章样式上体现)。 */
function subtitleLabel(stream: SubtitleStream): string {
  const codec = stream.codec
    ? (SUBTITLE_CODEC_LABELS[stream.codec.toLowerCase()] ?? stream.codec.toUpperCase())
    : null;
  const name = languageLabel(stream.language) ?? stream.title ?? null;
  const parts = [name, codec].filter(Boolean);
  if (stream.forced) parts.push("强制");
  return parts.join(" · ") || "未知字幕";
}

function videoCodecLabel(codec: string | null): string | null {
  if (!codec) return null;
  return VIDEO_CODEC_LABELS[codec.toLowerCase()] ?? codec.toUpperCase();
}

/** 一个文件的视频规格徽章集合（详情各处共用同一套语言）。 */
function videoBadges(file: LibraryItemFile): string[] {
  const badges: (string | null)[] = [
    file.resolution,
    videoCodecLabel(file.video_codec),
    file.hdr,
    file.bit_depth && file.bit_depth > 8 ? `${file.bit_depth}bit` : null,
    file.bit_rate ? `${(file.bit_rate / 1_000_000).toFixed(1)} Mbps` : null,
    file.container ? file.container.toUpperCase() : null,
    file.media_source,
    file.release_group,
  ];
  return badges.filter((b): b is string => Boolean(b));
}

/* ------------------------------------------------------------------------ */
/* 子组件                                                                     */
/* ------------------------------------------------------------------------ */

/** 头部元信息行的质量徽章：全部文件去重后的分辨率/HDR 概览。 */
function QualityBadges({ files }: { files: LibraryItemFile[] }) {
  const labels = useMemo(() => {
    const set = new Set<string>();
    for (const f of files) {
      if (f.resolution) set.add(f.resolution);
      if (f.hdr) set.add(f.hdr);
    }
    return [...set];
  }, [files]);
  if (labels.length === 0) return null;
  return (
    <span className="flex gap-1.5">
      {labels.map((b) => (
        <span
          key={b}
          className="rounded border border-white/25 px-1.5 py-px text-micro font-semibold tracking-wide text-white/85"
        >
          {b}
        </span>
      ))}
    </span>
  );
}

/**
 * 电影片源规格：多版本（1080p 与 2160p 并存）合并成切换器，选中版本
 * 展开视频 / 音轨 / 字幕三组真实规格（探测自文件本体，不来自种子名）。
 */
function MovieVersionSpecs({ files }: { files: LibraryItemFile[] }) {
  // 在位版本优先、分辨率高在前
  const versions = useMemo(
    () =>
      [...files].sort((a, b) => {
        if (a.missing !== b.missing) return a.missing ? 1 : -1;
        return (b.resolution ?? "").localeCompare(a.resolution ?? "") || b.size_bytes - a.size_bytes;
      }),
    [files],
  );
  const [activeId, setActiveId] = useState(versions[0]?.id);
  const active = versions.find((f) => f.id === activeId) ?? versions[0];
  if (!active) return null;

  const versionLabel = (f: LibraryItemFile) =>
    [f.resolution ?? f.container?.toUpperCase() ?? "未知规格", videoCodecLabel(f.video_codec), formatBytes(f.size_bytes)]
      .filter(Boolean)
      .join(" · ");

  return (
    <section>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          片源规格
        </h2>
        {versions.length > 1 && (
          <div className="flex flex-wrap gap-1.5">
            {versions.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={f.id === active.id}
                onClick={() => setActiveId(f.id)}
                className={`tnum rounded-full px-3 py-1 text-sub font-medium transition-colors ${
                  f.id === active.id
                    ? "bg-white/[0.14] text-white"
                    : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
                }`}
              >
                {versionLabel(f)}
                {f.missing ? "（缺失）" : ""}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-6 backdrop-blur-xl">
        <SpecRows file={active} />
      </div>
    </section>
  );
}

/** 一个文件的规格三行：视频 / 音频 / 字幕（文件区逐集展开时共用）。 */
function SpecRows({ file }: { file: LibraryItemFile }) {
  return (
    <div className="space-y-4">
      <SpecRow label="视频">
        {videoBadges(file).length > 0 ? (
          videoBadges(file).map((b) => <SpecBadge key={b} text={b} />)
        ) : (
          <span className="text-sub text-[var(--text-muted)]">未能探测（ffprobe 缺失或文件不可达）</span>
        )}
      </SpecRow>
      <SpecRow label="音频">
        {file.audio_streams === null ? (
          <span className="text-sub text-[var(--text-muted)]">
            尚未探测——后台正在读取，稍候自动刷新；持续为空请检查文件是否可达，
            以及（源码部署时）是否装了 ffmpeg
          </span>
        ) : file.audio_streams.length === 0 ? (
          <span className="text-sub text-[var(--text-muted)]">文件内没有音轨</span>
        ) : (
          file.audio_streams.map((a, i) => (
            <SpecBadge key={i} text={audioLabel(a)} accent={a.default} />
          ))
        )}
      </SpecRow>
      <SpecRow label="字幕">
        {file.subtitle_streams.length === 0 ? (
          <span className="text-sub text-[var(--text-muted)]">无内封或外挂字幕</span>
        ) : (
          file.subtitle_streams.map((s, i) => (
            <SpecBadge
              key={i}
              text={subtitleLabel(s)}
              suffix={s.external ? "外挂" : undefined}
            />
          ))
        )}
      </SpecRow>
    </div>
  );
}

function SpecRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-4">
      <span className="w-10 shrink-0 text-sub text-[var(--text-faint)]">{label}</span>
      <div className="flex flex-wrap items-center gap-1.5">{children}</div>
    </div>
  );
}

function SpecBadge({
  text,
  accent = false,
  suffix,
}: {
  text: string;
  accent?: boolean;
  suffix?: string;
}) {
  return (
    <span
      className={`tnum inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-caption ${
        accent
          ? "border-white/30 bg-white/[0.1] text-white"
          : "border-white/[0.12] bg-white/[0.04] text-white/80"
      }`}
    >
      {text}
      {suffix && (
        <span className="rounded-sm bg-white/[0.12] px-1 text-micro text-white/70">{suffix}</span>
      )}
    </span>
  );
}

/** 外部信息源链接：新窗口打开站点词条，样式与「在 TMDB 打开核对」保持一致。 */
function SourceLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-caption text-[var(--text-muted)] underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
    >
      {label} ↗
    </a>
  );
}

/**
 * 季号 → 展示名（0 是特别篇，TMDB 的 specials 惯例）。整季一个文件都没有的
 * 季标"未入库"：季选择器里元数据的季和实有的季混在一起，不标出来用户会以为
 * 自己本地存了那么多季。原生 select 收起时显示的就是选中项的文本，所以这个
 * 后缀在展开和收起两种状态下都看得到。
 */
function seasonLabel(season: number, owned: boolean): string {
  const name = season === 0 ? "特别篇" : `第 ${season} 季`;
  return owned ? name : `${name} · 未入库`;
}

/**
 * 剧集分集区（播放器式）：季选择器 + 分集横滚缩略图卡（剧照 + 集名，
 * 缺集置灰），点选一集在下方展开该集的简介、真实规格与物理文件——
 * 剧集的浏览心智是"看这季有哪些集、这集是什么规格"，与电影的版本切换
 * 是两套逻辑。分集信息本地刮削（分集 NFO/缩略图）优先，TMDB 分季兜底。
 */
function SeasonEpisodesSection({
  libraryId,
  detail,
  onDeleteFile,
}: {
  libraryId: number;
  detail: LibraryItemDetail;
  onDeleteFile: (file: LibraryItemFile) => void;
}) {
  const seasons = detail.seasons;
  // 季选择器列的是「元数据的季 ∪ 库里实有的季」，本地没有的季也在里面（看得到
  // 缺口）。所以要单独算出哪些季真在库——口径与后端 owned_seasons 一致：台账
  // 文件的季号集合。
  const ownedSeasons = useMemo(
    () => new Set(detail.files.map((f) => f.season_number)),
    [detail.files],
  );
  // 默认落在第一个在库的季，而不是季号最小的那一季：只存了第 5、6 季的剧，
  // 打开详情页就停在满屏置灰的第 1 季，第一眼像"我的片子没了"
  const [season, setSeason] = useState(
    () => seasons.find((s) => ownedSeasons.has(s)) ?? seasons[0],
  );
  const [data, setData] = useState<SeasonEpisodes | null>(null);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setFailed(false);
    getItemEpisodes(libraryId, detail.media_item_id, season)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        // 默认选中第一个在库的集（都不在库时选第一集，仍可看简介）
        const first = result.episodes.find((e) => e.owned) ?? result.episodes[0];
        setSelected(first?.episode_number ?? null);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
    // detail.file_count：单文件删除后详情重拉，分集的 owned/file_ids 也要
    // 跟着刷新（用文件数而不是整个 detail 做依赖——刮削轮询也会换 detail
    // 对象，不该每轮都重拉分集）
  }, [libraryId, detail.media_item_id, detail.file_count, season]);

  const filesById = useMemo(
    () => new Map(detail.files.map((f) => [f.id, f])),
    [detail.files],
  );
  const current = data?.episodes.find((e) => e.episode_number === selected) ?? null;
  const currentFiles = (current?.file_ids ?? [])
    .map((id) => filesById.get(id))
    .filter((f): f is LibraryItemFile => Boolean(f));
  const ownedCount = data ? data.episodes.filter((e) => e.owned).length : 0;

  return (
    <section>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
          分集
        </h2>
        {seasons.length > 1 ? (
          <select
            value={season}
            onChange={(e) => setSeason(Number(e.target.value))}
            className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-sub text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
          >
            {seasons.map((s) => (
              <option key={s} value={s}>
                {seasonLabel(s, ownedSeasons.has(s))}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-sub text-[var(--text-muted)]">
            {seasonLabel(season, ownedSeasons.has(season))}
          </span>
        )}
        {data && (
          <span className="tnum text-sub text-[var(--text-faint)]">
            在库 {ownedCount} / {data.episodes.length} 集
          </span>
        )}
      </div>

      {failed && (
        <p className="text-sub text-[var(--text-muted)]">分集信息加载失败，请稍后重试。</p>
      )}
      {!data && !failed && (
        <div className="flex items-center gap-2.5 py-6 text-sub text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在读取分集信息…
        </div>
      )}

      {data && (
        <>
          {/* 分集横滚卡：16:9 剧照 + "N. 集名"；缺集置灰，选中亮环。
              走 HScroller 以复用发现页海报行的左右翻页钮，避免用户看不出这行可横滑 */}
          <HScroller className="-mx-1 gap-3 px-1 pb-1 pt-1">
            {data.episodes.map((episode) => (
              <EpisodeCard
                key={episode.episode_number}
                episode={episode}
                selected={episode.episode_number === selected}
                onSelect={() => setSelected(episode.episode_number)}
              />
            ))}
          </HScroller>

          {/* 选中集面板：简介 + 该集文件的真实规格与物理位置 */}
          {current && (
            <div className="mt-4 rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] p-6 backdrop-blur-xl">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="text-body font-semibold text-[var(--text)]">
                  第 {current.episode_number} 集{current.name ? ` · ${current.name}` : ""}
                </h3>
                {current.air_date && (
                  <span className="tnum text-sub text-[var(--text-faint)]">
                    {current.air_date} 播出
                  </span>
                )}
                {!current.owned && (
                  <span className="rounded border border-[#f5c451]/40 px-1.5 py-px text-micro font-semibold text-[#f5c451]">
                    不在库中
                  </span>
                )}
              </div>
              {current.overview && (
                <p className="mt-2.5 max-w-3xl text-ui leading-6 text-white/70">
                  {current.overview}
                </p>
              )}
              {currentFiles.length > 0 ? (
                <div className="mt-4 space-y-4">
                  {currentFiles.map((file) => (
                    <div
                      key={file.id}
                      className="group/epfile rounded-xl border border-white/[0.06] bg-white/[0.03] p-4"
                    >
                      <div className="flex items-center gap-4">
                        <Tooltip
                          content={
                            <span className="tnum break-all font-mono text-caption leading-5">
                              {file.file_path}
                            </span>
                          }
                          maxWidth={520}
                        >
                          <p className="min-w-0 flex-1 truncate text-ui text-[var(--text)]">
                            {file.file_name}
                          </p>
                        </Tooltip>
                        {file.missing && (
                          <span className="shrink-0 rounded border border-[#f5c451]/40 px-1.5 py-px text-micro font-semibold text-[#f5c451]">
                            文件缺失
                          </span>
                        )}
                        <span className="shrink-0 rounded border border-white/[0.14] px-1.5 py-px text-micro text-white/55">
                          {file.source === "imported" ? "入库管线" : "扫描发现"}
                        </span>
                        <span className="tnum shrink-0 text-sub text-[var(--text-muted)]">
                          {formatBytes(file.size_bytes)}
                        </span>
                        <span className="tnum shrink-0 text-caption text-[var(--text-faint)]">
                          {formatRelativeTime(file.added_at)}
                        </span>
                        <button
                          type="button"
                          aria-label="删除此文件"
                          title="删除此文件"
                          onClick={() => onDeleteFile(file)}
                          className="touch-reveal shrink-0 rounded-md p-1.5 text-[var(--text-faint)] opacity-0 transition group-hover/epfile:opacity-100 hover:bg-white/[0.06] hover:text-[#ff9f9f]"
                        >
                          <TrashIcon className="size-4" />
                        </button>
                      </div>
                      <div className="mt-3.5">
                        <SpecRows file={file} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-3 text-sub text-[var(--text-muted)]">
                  本集不在库中——可通过订阅追踪自动补齐。
                </p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}

/** 分集横滚卡：16:9 剧照缩略图 + 集号集名；缺集置灰。 */
function EpisodeCard({
  episode,
  selected,
  onSelect,
}: {
  episode: LibraryEpisode;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`w-[200px] shrink-0 rounded-xl text-left outline-none transition ${
        episode.owned ? "" : "opacity-45 saturate-50"
      }`}
    >
      <div
        className={`relative aspect-video overflow-hidden rounded-xl bg-[#141824] transition ${
          selected
            ? "ring-2 ring-white/85 shadow-[0_10px_30px_rgba(0,0,0,0.5)]"
            : "ring-1 ring-white/[0.08] hover:ring-white/35"
        }`}
      >
        <PosterImage
          src={imageUrl(episode.still_url)}
          alt={`第 ${episode.episode_number} 集剧照`}
          className="size-full object-cover"
          fallback={
            <span className="tnum flex size-full items-center justify-center text-[22px] font-bold text-white/20">
              {episode.episode_number}
            </span>
          }
        />
        {!episode.owned && (
          <span className="absolute right-1.5 top-1.5 rounded bg-black/60 px-1.5 py-px text-micro font-semibold text-[#f5c451]">
            缺
          </span>
        )}
      </div>
      <p
        className={`tnum mt-1.5 truncate text-sub ${
          selected ? "font-semibold text-white" : "text-[var(--text)]"
        }`}
      >
        {episode.episode_number}. {episode.name ?? `第 ${episode.episode_number} 集`}
      </p>
    </button>
  );
}

/**
 * 文件区：这份库存到底是哪些物理文件。原始文件名 + 尺寸常显，悬浮文件名
 * 看完整物理路径（识别错挂时用户第一时间要知道"这是哪个文件"）；
 * 电影列全部文件，剧集只列没归到集的零散文件。
 */
function FileSection({
  files,
  isMovie,
  title,
  onDeleteFile,
}: {
  files: LibraryItemFile[];
  isMovie: boolean;
  title: string;
  onDeleteFile: (file: LibraryItemFile) => void;
}) {
  // 剧集按季分组；电影全部落在 0 组
  const groups = useMemo(() => {
    const bySeason = new Map<number, LibraryItemFile[]>();
    for (const f of files) {
      const list = bySeason.get(f.season_number) ?? [];
      list.push(f);
      bySeason.set(f.season_number, list);
    }
    return [...bySeason.entries()].sort((a, b) => a[0] - b[0]);
  }, [files]);

  return (
    <section>
      <h2 className="text-on-image mb-3 text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]">
        {title}{" "}
        <span className="tnum text-ui font-normal text-[var(--text-muted)]">{files.length}</span>
      </h2>
      <div className="overflow-hidden rounded-2xl border border-white/[0.07] bg-[rgba(14,16,22,0.45)] backdrop-blur-xl">
        {groups.map(([season, groupFiles]) => (
          <div key={season}>
            {!isMovie && groups.length > 1 && (
              <p className="border-b border-white/[0.05] px-5 pb-2 pt-3.5 text-sub font-semibold text-[var(--text-muted)]">
                {season === 0 ? "未归集" : `第 ${season} 季`} · {groupFiles.length} 个文件
              </p>
            )}
            {groupFiles.map((file) => (
              <FileRow
                key={file.id}
                file={file}
                isMovie={isMovie}
                onDelete={() => onDeleteFile(file)}
              />
            ))}
          </div>
        ))}
      </div>
      <p className="mt-2 text-caption text-[var(--text-faint)]">
        悬浮文件名可查看物理路径。规格探测自文件本体（ffprobe），不来自资源命名。
      </p>
    </section>
  );
}

function FileRow({
  file,
  isMovie,
  onDelete,
}: {
  file: LibraryItemFile;
  isMovie: boolean;
  onDelete: () => void;
}) {
  // 剧集行可展开看逐集完整规格（电影已有片源规格区，这里保持单行紧凑）
  const [expanded, setExpanded] = useState(false);
  const badges = videoBadges(file).slice(0, 4);
  const audioSummary =
    file.audio_streams && file.audio_streams.length > 0
      ? audioLabel(file.audio_streams[0]) +
        (file.audio_streams.length > 1 ? ` 等 ${file.audio_streams.length} 轨` : "")
      : null;

  return (
    <div className="border-b border-white/[0.05] last:border-b-0">
      <div
        className={`group/filerow flex items-center gap-4 px-5 py-3 ${!isMovie ? "cursor-pointer hover:bg-white/[0.03]" : ""}`}
        onClick={!isMovie ? () => setExpanded((v) => !v) : undefined}
      >
        <div className="min-w-0 flex-1">
          <Tooltip
            content={
              <span className="tnum break-all font-mono text-caption leading-5">
                {file.file_path}
              </span>
            }
            maxWidth={520}
          >
            <p className="truncate text-ui text-[var(--text)]">
              {!isMovie && (file.episode_number > 0 || file.season_number > 0) && (
                <span className="tnum mr-2 text-sub font-semibold text-[var(--accent-2)]">
                  S{String(file.season_number).padStart(2, "0")}E
                  {String(file.episode_number).padStart(2, "0")}
                </span>
              )}
              {file.file_name}
            </p>
          </Tooltip>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-caption text-[var(--text-muted)]">
            {badges.length > 0 && <span className="tnum">{badges.join(" · ")}</span>}
            {audioSummary && <span className="tnum">{audioSummary}</span>}
            {file.subtitle_streams.length > 0 && (
              <span>{file.subtitle_streams.length} 条字幕</span>
            )}
          </p>
        </div>
        {file.missing && (
          <span className="shrink-0 rounded border border-[#f5c451]/40 px-1.5 py-px text-micro font-semibold text-[#f5c451]">
            文件缺失
          </span>
        )}
        <span className="shrink-0 rounded border border-white/[0.14] px-1.5 py-px text-micro text-white/55">
          {file.source === "imported" ? "入库管线" : "扫描发现"}
        </span>
        <span className="tnum w-20 shrink-0 text-right text-sub text-[var(--text-muted)]">
          {formatBytes(file.size_bytes)}
        </span>
        <span className="tnum w-20 shrink-0 text-right text-caption text-[var(--text-faint)]">
          {formatRelativeTime(file.added_at)}
        </span>
        <button
          type="button"
          aria-label="删除此文件"
          title="删除此文件"
          onClick={(e) => {
            e.stopPropagation(); // 剧集行点击是展开规格，别让删除误触发展开
            onDelete();
          }}
          className="touch-reveal shrink-0 rounded-md p-1.5 text-[var(--text-faint)] opacity-0 transition group-hover/filerow:opacity-100 hover:bg-white/[0.06] hover:text-[#ff9f9f]"
        >
          <TrashIcon className="size-4" />
        </button>
      </div>
      {expanded && !isMovie && (
        <div className="border-t border-white/[0.04] bg-white/[0.02] px-5 py-4">
          <SpecRows file={file} />
        </div>
      )}
    </div>
  );
}

/**
 * 转移到其他库的弹窗：选库 → 预览 → 确认 → 进度 → 结论，四步走完一条路。
 *
 * 存在的理由：入库选库要么靠收藏范围自动路由、要么靠订阅时手选，两条路
 * 都会错——最典型的是韩剧被判进了「大陆华语剧」。此前对这种错分没有任何
 * 补救手段，只能手工挪目录再两边重扫。
 *
 * 界面上必须讲清的三件事（都在预览步骤里）：
 * 1. **搬的是磁盘目录**，不是"从列表里挪一下"——把源目录与目标目录并排列出；
 * 2. **跨盘要复制**：目标与源不在同一块盘时是完整复制而非改名，耗时按体积
 *    走，且会断开与做种目录的硬链接（磁盘占用翻倍）——这条必须显式警示；
 * 3. **目标已有同名目录就不干**：宁可让用户自己决断，绝不覆盖或合并。
 */
function TransferDialog({
  open,
  detail,
  libraryId,
  sourceLibraryName,
  onClose,
  onTransferred,
}: {
  open: boolean;
  detail: LibraryItemDetail;
  libraryId: number;
  sourceLibraryName: string | null;
  onClose: () => void;
  onTransferred: (targetLibraryId: number) => void;
}) {
  // 可选目标库：同类型、非当前库（类型不同是"认错了片子"，该走重新识别）
  const [candidates, setCandidates] = useState<MediaLibrary[] | null>(null);
  const [targetId, setTargetId] = useState<number | null>(null);
  const [preview, setPreview] = useState<TransferPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 进行中/已完成的转移状态（后台任务，轮询同一个接口一路走到结论）
  const [status, setStatus] = useState<TransferStatus | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTargetId(null);
    setPreview(null);
    setStatus(null);
    setError(null);
    listLibraries(detail.kind)
      .then((libs) => setCandidates(libs.filter((l) => l.id !== libraryId)))
      .catch(() => setError("读取媒体库列表失败，请稍后重试"));
  }, [open, detail.kind, libraryId]);

  // 选中目标库就立刻算预览：用户要先看清"搬到哪、搬多少"才谈得上确认
  useEffect(() => {
    if (!open || targetId == null) return;
    let alive = true;
    setLoadingPreview(true);
    setPreview(null);
    setError(null);
    previewItemTransfer(libraryId, detail.media_item_id, targetId)
      .then((data) => alive && setPreview(data))
      .catch((err) => alive && setError(err instanceof Error ? err.message : "预览失败"))
      .finally(() => alive && setLoadingPreview(false));
    return () => {
      alive = false;
    };
  }, [open, targetId, libraryId, detail.media_item_id]);

  // 转移进行中：每秒拉一次进度，跑完自动切到结论页（不用 useVisiblePolling——
  // 这是用户正盯着看的短任务，页面切后台也该继续走完，回来直接看到结果）
  const running = status?.running ?? false;
  useEffect(() => {
    if (!open || !running) return;
    const timer = setInterval(() => {
      getTransferStatus(libraryId)
        .then(setStatus)
        .catch(() => {});
    }, 1000);
    return () => clearInterval(timer);
  }, [open, running, libraryId]);

  const run = async () => {
    if (targetId == null) return;
    setStarting(true);
    setError(null);
    try {
      await transferLibraryItem(libraryId, detail.media_item_id, targetId);
      setStatus(await getTransferStatus(libraryId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "转移失败，请稍后重试");
    } finally {
      setStarting(false);
    }
  };

  const busy = starting || running;
  const finished = status != null && !status.running;
  const canRun =
    preview != null && preview.blocked.length === 0 && preview.moves.length > 0 && !busy;

  return (
    <Modal
      open={open}
      onClose={busy ? () => {} : onClose}
      label="转移到其他媒体库"
      width="2xl"
      panelClassName="flex max-h-[80vh] flex-col"
    >
      <div className="scroll-thin flex-1 overflow-y-auto p-6">
        {finished ? (
          <TransferResult status={status!} />
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <FolderIcon className="size-4.5 text-[var(--accent-2)]" />
              把「{detail.title}」转移到其他媒体库
            </h3>
            <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
              分错库时用它补救（例如韩剧被判进了「大陆华语剧」）。
              <span className="text-white/80">磁盘上的整个条目目录</span>
              （视频、NFO、海报、字幕）会连同库存记录一起搬到目标库；
              目录名原样保留，需要规范化请到目标库运行「整理文件名」。
            </p>

            {/* —— 第一步：选目标库 —— */}
            <p className="mt-5 text-caption font-semibold uppercase tracking-[0.16em] text-[var(--text-faint)]">
              转移到
            </p>
            {candidates == null ? (
              <p className="mt-2 text-sub text-[var(--text-muted)]">正在读取媒体库…</p>
            ) : candidates.length === 0 ? (
              <p className="mt-2 text-sub leading-6 text-[#ffd08a]">
                没有其他{detail.kind === "movie" ? "电影" : "剧集"}
                库可选——请先在「媒体库」页新建一个，再回来转移。
              </p>
            ) : (
              <div className="mt-2 space-y-1.5">
                {candidates.map((lib) => (
                  <button
                    key={lib.id}
                    type="button"
                    disabled={busy}
                    onClick={() => setTargetId(lib.id)}
                    className={`flex w-full items-center gap-3 rounded-xl border px-3.5 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${
                      targetId === lib.id
                        ? "border-[var(--accent-2)]/60 bg-[var(--accent-2)]/[0.1]"
                        : "border-white/[0.08] bg-white/[0.03] hover:bg-white/[0.06]"
                    }`}
                  >
                    <span
                      className={`size-3.5 shrink-0 rounded-full border-2 ${
                        targetId === lib.id
                          ? "border-[var(--accent-2)] bg-[var(--accent-2)]"
                          : "border-white/25"
                      }`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block text-ui font-medium text-[var(--text)]">
                        {lib.name}
                        {lib.is_default && (
                          <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                            默认库
                          </span>
                        )}
                      </span>
                      <span className="block truncate font-mono text-caption text-white/45">
                        {lib.primary_root ?? "未配置根路径"}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}

            {/* —— 第二步：预览「将要发生什么」 —— */}
            {loadingPreview && (
              <p className="mt-4 flex items-center gap-2 text-sub text-[var(--text-muted)]">
                <span className="size-3.5 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
                正在计算转移计划…
              </p>
            )}
            {preview && <TransferPlanPreview preview={preview} sourceName={sourceLibraryName} />}
            {error && <p className="mt-3 text-sub leading-6 text-[#ff9f9f]">{error}</p>}
          </>
        )}
      </div>

      {/* —— 底栏：进行中只留进度，不给"取消"（搬到一半停不下来）—— */}
      <div className="flex items-center gap-3 border-t border-white/[0.07] px-6 py-4">
        {running && (
          <span className="flex min-w-0 flex-1 items-center gap-2 text-sub text-[#7dd3fc]">
            <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-[#7dd3fc]/30 border-t-[#7dd3fc]" />
            <span className="truncate">
              正在转移
              {status && status.total > 0 ? ` · ${status.processed}/${status.total}` : ""}
              ；跨盘转移需要完整复制文件，请勿关闭页面以外的操作
            </span>
          </span>
        )}
        <div className="ml-auto flex gap-3">
          {finished ? (
            <>
              <button
                type="button"
                onClick={onClose}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                留在本页
              </button>
              {status!.errors.length === 0 && status!.target_library_id != null && (
                <button
                  type="button"
                  onClick={() => onTransferred(status!.target_library_id!)}
                  className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
                >
                  去目标库查看
                </button>
              )}
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)] disabled:opacity-40"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!canRun}
                className="btn-accent flex items-center gap-2 rounded-full px-5 py-2 text-ui font-semibold disabled:cursor-not-allowed disabled:opacity-40"
              >
                {starting && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                确认转移
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

/** 转移计划预览：目标位置、体积、跨盘警示、阻断原因与跳过说明。 */
function TransferPlanPreview({
  preview,
  sourceName,
}: {
  preview: TransferPreview;
  sourceName: string | null;
}) {
  return (
    <div className="mt-4 space-y-3">
      {preview.blocked.length > 0 && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/[0.08] px-4 py-3 text-sub leading-6 text-[#ffb4b4]">
          {preview.blocked.map((b) => (
            <p key={b}>{b}</p>
          ))}
        </div>
      )}

      {preview.cross_device && preview.moves.length > 0 && (
        <div className="rounded-xl border border-[#ffd08a]/30 bg-[#ffd08a]/[0.08] px-4 py-3 text-sub leading-6 text-[#ffd08a]">
          目标库与当前位置<span className="font-semibold">不在同一块盘</span>
          ：文件需要完整复制（{formatBytes(preview.total_bytes)}），耗时取决于体积与盘速；
          复制会产生新文件，与下载器做种目录的硬链接关系将断开，两边各占一份空间。
        </div>
      )}

      {preview.moves.length > 0 && (
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
          <p className="text-caption text-[var(--text-faint)]">
            {preview.moves.length} 个路径 · {formatBytes(preview.total_bytes)}
            {preview.missing_count > 0 && ` · ${preview.missing_count} 个缺失记录随迁`}
          </p>
          <div className="scroll-thin mt-2 max-h-44 space-y-2 overflow-y-auto">
            {preview.moves.map((m) => (
              <div key={m.source_path} className="tnum font-mono text-caption leading-5">
                <p className="break-all text-white/45">
                  {sourceName ? `${sourceName} · ` : ""}
                  {m.source_path}
                </p>
                <p className="break-all text-white/80">
                  ↳ {preview.target_library_name} · {m.target_path}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview.skips.length > 0 && (
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.02] p-3.5 text-caption leading-5 text-white/60">
          {preview.skips.map((s) => (
            <p key={s.file_path} className="break-all py-0.5">
              <span className="font-mono">{s.file_path}</span>：{s.reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/** 转移结论页：搬了哪些路径、随迁多少台账、订阅是否跟着改挂。 */
function TransferResult({ status }: { status: TransferStatus }) {
  const failed = status.errors.length > 0;
  return (
    <>
      <h3 className="text-title-sm font-semibold text-[var(--text)]">
        {failed ? "部分转移完成" : `「${status.title}」已转移到「${status.target_library_name}」`}
      </h3>
      <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
        {status.moved_paths.map((p) => (
          <p
            key={p}
            className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70"
          >
            {p}
          </p>
        ))}
        {status.moved_paths.length === 0 && (
          <p className="text-sub text-[var(--text-muted)]">没有搬运任何磁盘路径</p>
        )}
      </div>
      {failed && (
        <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
          {status.errors.map((e) => (
            <p key={e}>{e}</p>
          ))}
        </div>
      )}
      <p className="mt-3 text-sub leading-6 text-[var(--text-muted)]">
        已随迁 {status.files_relocated} 条库存记录（{formatBytes(status.bytes_moved)}）
        {status.removed_dirs > 0 && `，清理空目录 ${status.removed_dirs} 个`}。
        {status.subscription_moved && "该片的订阅已一并改挂到目标库，后续剧集直接入新库。"}
      </p>
    </>
  );
}

/**
 * 删除确认弹窗：全站唯一真删磁盘的操作，把后果一次性讲透——
 * 列出将被清掉的目录、文件数与体积，勾选确认后才放开红色按钮；
 * 完成后展示实际删除的路径清单，让用户对"删了什么"有完整凭据。
 */
function DeleteDialog({
  open,
  detail,
  libraryId,
  onClose,
  onDeleted,
}: {
  open: boolean;
  detail: LibraryItemDetail;
  libraryId: number;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ItemDeleteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setResult(null);
      setError(null);
    }
  }, [open]);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await deleteLibraryItem(libraryId, detail.media_item_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} label="删除影片" width="lg">
      <div className="p-6">
        {result ? (
          <>
            <h3 className="text-title-sm font-semibold text-[var(--text)]">
              {result.errors.length > 0 ? "部分删除完成" : "已从磁盘彻底删除"}
            </h3>
            <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {result.removed_paths.map((p) => (
                <p key={p} className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  {p}
                </p>
              ))}
              {result.removed_paths.length === 0 && (
                <p className="text-sub text-[var(--text-muted)]">没有删除任何磁盘路径</p>
              )}
            </div>
            {result.errors.length > 0 && (
              <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
                {result.errors.map((e) => (
                  <p key={e}>{e}</p>
                ))}
              </div>
            )}
            <p className="mt-3 text-sub text-[var(--text-muted)]">
              已清理 {result.rows_deleted} 条台账，释放 {formatBytes(result.freed_bytes)}。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={onDeleted}
                className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
              >
                回到库存页
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <TrashIcon className="size-4.5 text-[#ff9f9f]" />
              删除「{detail.title}」
            </h3>
            <p className="mt-3 text-ui leading-6 text-white/80">
              这不是「从列表移除」——将把下列目录从磁盘
              <span className="font-semibold text-[#ff9f9f]">彻底删除</span>
              ，包括视频文件、NFO、海报、字幕等全部刮削产物，共{" "}
              <span className="tnum font-semibold">{detail.file_count}</span> 个媒体文件、
              <span className="tnum font-semibold"> {formatBytes(detail.total_size_bytes)}</span>
              。此操作不可恢复。
            </p>
            <div className="scroll-thin mt-4 max-h-40 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {(detail.entry_dirs.length > 0
                ? detail.entry_dirs
                : detail.files.map((f) => f.file_path)
              ).map((p) => (
                <p key={p} className="tnum flex items-start gap-1.5 break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  <FolderIcon className="mt-0.5 size-3.5 shrink-0 text-white/40" />
                  {p}
                </p>
              ))}
            </div>
            <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-sub leading-6 text-white/80">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                className="mt-1 size-4 accent-[#ff6b6b]"
              />
              我已明白：以上目录及其中全部文件将被永久删除，无法恢复。
            </label>
            {error && <p className="mt-3 text-sub text-[#ff9f9f]">{error}</p>}
            <div className="mt-5 flex justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!confirmed || busy}
                className="flex items-center gap-2 rounded-full bg-[#c73838] px-5 py-2 text-ui font-semibold text-white transition hover:bg-[#d64545] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                {busy ? "正在删除…" : "彻底删除"}
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

/**
 * 单文件删除确认弹窗：条目删除的文件级姊妹（多版本洗掉一个 / 删某集重下）。
 * 与 DeleteDialog 同一套三段式：讲透后果 → 勾选确认 → 红色按钮；
 * 完成后展示实际删除的路径清单。两条必须讲清的规则：
 * 1. 最后一个文件会升级为整条目删除（后端行为，不留只剩 NFO/海报的空目录）；
 * 2. 该单元有订阅盯着时，删掉最后一份拷贝订阅会自动重新下载补齐。
 */
function DeleteFileDialog({
  file,
  detail,
  libraryId,
  onClose,
  onDeleted,
}: {
  file: LibraryItemFile | null;
  detail: LibraryItemDetail;
  libraryId: number;
  onClose: () => void;
  onDeleted: (deletedItem: boolean) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ItemDeleteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 条目在本库只剩这一行台账（含 missing 行）→ 后端会升级为整条目删除
  const isLast = detail.files.length === 1;

  useEffect(() => {
    if (file) {
      setConfirmed(false);
      setResult(null);
      setError(null);
    }
  }, [file]);

  if (!file) return null;

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await deleteLibraryFile(libraryId, detail.media_item_id, file.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={busy ? () => {} : onClose} label="删除文件" width="lg">
      <div className="p-6">
        {result ? (
          <>
            <h3 className="text-title-sm font-semibold text-[var(--text)]">
              {result.errors.length > 0 ? "删除失败" : "已从磁盘删除"}
            </h3>
            <div className="scroll-thin mt-4 max-h-56 overflow-y-auto rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              {result.removed_paths.map((p) => (
                <p key={p} className="tnum break-all py-0.5 font-mono text-caption leading-5 text-white/70">
                  {p}
                </p>
              ))}
              {result.removed_paths.length === 0 && (
                <p className="text-sub text-[var(--text-muted)]">
                  没有删除任何磁盘路径{file.missing ? "（文件本就缺失，仅清除了台账记录）" : ""}
                </p>
              )}
            </div>
            {result.errors.length > 0 && (
              <div className="mt-3 space-y-1 text-sub leading-5 text-[#ff9f9f]">
                {result.errors.map((e) => (
                  <p key={e}>{e}</p>
                ))}
              </div>
            )}
            <p className="mt-3 text-sub text-[var(--text-muted)]">
              已清理 {result.rows_deleted} 条台账，释放 {formatBytes(result.freed_bytes)}。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={() => onDeleted(isLast && result.errors.length === 0)}
                className="btn-accent rounded-full px-5 py-2 text-ui font-semibold"
              >
                完成
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="flex items-center gap-2 text-title-sm font-semibold text-[var(--text)]">
              <TrashIcon className="size-4.5 text-[#ff9f9f]" />
              删除文件
            </h3>
            <p className="mt-3 text-ui leading-6 text-white/80">
              {file.missing ? (
                <>该文件在磁盘上已缺失，删除只会清掉这条台账记录。</>
              ) : (
                <>
                  将把下列文件从磁盘
                  <span className="font-semibold text-[#ff9f9f]">彻底删除</span>
                  ，同名的 NFO/字幕/图片附属文件一并清除。此操作不可恢复。
                </>
              )}
            </p>
            <div className="mt-4 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3.5">
              <p className="text-ui text-[var(--text)]">
                {detail.kind !== "movie" &&
                  (file.episode_number > 0 || file.season_number > 0) && (
                    <span className="tnum mr-2 text-sub font-semibold text-[var(--accent-2)]">
                      S{String(file.season_number).padStart(2, "0")}E
                      {String(file.episode_number).padStart(2, "0")}
                    </span>
                  )}
                {file.file_name}
                <span className="tnum ml-2 text-sub text-[var(--text-muted)]">
                  {formatBytes(file.size_bytes)}
                </span>
              </p>
              <p className="tnum mt-1 break-all font-mono text-caption leading-5 text-white/50">
                {file.file_path}
              </p>
            </div>
            {isLast && (
              <p className="mt-3 rounded-xl border border-[#f5c451]/30 bg-[#f5c451]/[0.06] px-3.5 py-2.5 text-sub leading-6 text-[#f5c451]">
                这是「{detail.title}」在本库的最后一个文件——删除将升级为整条目删除，
                整个刮削目录（含 NFO/海报）一并清除，条目将从库存消失。
              </p>
            )}
            <p className="mt-3 text-caption leading-5 text-[var(--text-faint)]">
              若该作品有订阅且删除后此单元不再有其他拷贝，订阅会将其视为缺失并自动重新下载。
            </p>
            <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-sub leading-6 text-white/80">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                className="mt-1 size-4 accent-[#ff6b6b]"
              />
              我已明白：{isLast ? "整个条目目录及其中全部文件" : "该文件及其同名附属文件"}
              将被永久删除，无法恢复。
            </label>
            {error && <p className="mt-3 text-sub text-[#ff9f9f]">{error}</p>}
            <div className="mt-5 flex justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={run}
                disabled={!confirmed || busy}
                className="flex items-center gap-2 rounded-full bg-[#c73838] px-5 py-2 text-ui font-semibold text-white transition hover:bg-[#d64545] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                )}
                {busy ? "正在删除…" : "彻底删除"}
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
