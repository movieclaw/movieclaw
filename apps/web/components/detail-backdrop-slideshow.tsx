"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { MediaImage } from "@/lib/api/discover";

const ROTATE_INTERVAL_MS = 8000;
const FADE_MS = 1600;
// 与 themes/netflix/tokens.css 的 .detail-slideshow img 起步延迟（700ms）同源：
// 每张图挂载后先静止这么久才开始推镜，改两处必须一起改。
const KB_START_DELAY_MS = 700;
// 换底接续延迟的兜底换算值：仅在顶层动画读不到时使用（正常路径直接读顶层
// 动画的真实进度，见 cycle 内注释——定时器在标签页被节流时会大幅延迟，
// 按常数换算累计误差可达秒级，2026-09-21 节流环境下实测）。
const BOTTOM_CARRY_DELAY_S =
  (60 + FADE_MS + 100 - KB_START_DELAY_MS) / 1000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 从任意图片地址提取去重键：代理地址解开 url 参数取远端文件名，其余取路径
    尾段。同一张剧照的 w1280 与 original 共享同一远端文件名，键一致即同一张。 */
function imageKey(url: string): string {
  try {
    const proxied = url.match(/[?&]url=([^&]+)/);
    const raw = proxied ? decodeURIComponent(proxied[1]) : url;
    return raw.split("?")[0].split("/").pop() ?? raw;
  } catch {
    return url;
  }
}

/** 预加载并解码一张图；失败返回 false（该张本轮跳过）。 */
function load(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const img = new Image();
    const done = (ok: boolean) => {
      if (!ok) {
        resolve(false);
        return;
      }
      // 与沉浸覆盖层同款：解码完成才算就位，避免切上去那一帧空白
      img
        .decode()
        .catch(() => {})
        .then(() => resolve(true));
    };
    img.onload = () => done(true);
    img.onerror = () => done(false);
    img.src = url;
  });
}

/**
 * Netflix 详情页的背景轮换：剧照 original 原图按序做「叠变」——交叉溶解
 * （1.6s 双向淡入淡出）+ Ken Burns 缓推（globals.css 的 nf-kenburns：每张图
 * 在显示期间缓慢放大，溶解时两层各自在动，观感是「融接」而非「切换」）。
 * portal 到 body、z-2——**必须 portal**：放在详情树里会画在滚动容器的
 * 渐变板之上，横幅以下的本该全黑的区域会漏出图片。构图与覆盖层保持一致
 * （globals.css 的 .detail-slideshow，改构图两处必须一起改）。
 *
 * 规矩：
 *   - 首帧用 initialUrl（与沉浸覆盖层同源的主图，衔接零跳变），首轮换跳过
 *     清单里与首帧相同的照片（同图自溶解会读成「收缩+重影」，见 cycle 内
 *     注释），从第一张不同的照片起循环；
 *   - 驻留 8s 与发现页 Hero 同拍（HERO_INTERVAL=8000）：9s 推镜在下一张
 *     切入瞬间恰好推满，出场图不在 1.06 定格——定格后溶入一张从 1.0 起推
 *     的新图，合成画面会读成轻微收缩；
 *   - 下一张预加载并解码**就位才切**，加载失败跳过该张，绝不闪黑；
 *   - 页面不可见（切标签页）时跳过该轮，回来自动继续；
 *   - 系统开启「减弱动态效果」时不轮（本组件直接不挂载，见调用方）。
 *
 * 时序模型：轮换循环只跑**一份实例**（effect 依赖空数组 + refs 读参）。
 * 此前依赖 `urls` 数组——每次渲染都是新引用，任何一次重渲染都会重启 effect
 * 并取消进行中的循环，而循环第一步 setTop 本身就触发渲染：循环刚起步即被
 * 自己掐死，top 层永远停在 opacity-0（轮换「消失」的实测根因）。
 *
 * 双层交叉淡入：A/B 两个 <img> 叠放，B 淡入完成后把同一张图同步给 A、再把
 * B 摘掉——下一轮继续用 B 换图，任何时刻至多一层在过渡。
 */
export function DetailBackdropSlideshow({
  images,
  initialUrl,
  pushAnchor,
}: {
  images: MediaImage[];
  /** 首帧：与沉浸覆盖层当前显示的主图同源（主 backdrop 原图），衔接零跳变 */
  initialUrl?: string;
  /** 首帧覆盖层 Ken Burns 的起跑时刻（performance.now()）。首图与覆盖层是
      同一张照片，覆盖层又在推镜——首图必须接着它已推到的缩放继续，否则
      接管瞬间画面会「缩回去」一下。换算成负动画延迟后两层共享同一条 9s
      时间线，任何时刻缩放逐像素一致（图层淡入的 700ms 里也在动，无缝）。 */
  pushAnchor?: number;
}) {
  const [bottom, setBottom] = useState<string | null>(
    initialUrl ?? images[0]?.fullUrl ?? null,
  );
  const [top, setTop] = useState<string | null>(null);
  const [topOn, setTopOn] = useState(false);
  // 首图相位锁定的负延迟，挂载时刻一次性定格（元素整个生命周期不变）。
  // 已推超过 9s（覆盖层推完定格在 1.06）时负延迟超出时长，CSS 按「播完」
  // 处理直接停在 1.06——与定格中的覆盖层依然逐像素一致。
  const [firstPushDelay] = useState(() => {
    if (pushAnchor === undefined) return undefined;
    const elapsed = (performance.now() - pushAnchor) / 1000;
    if (elapsed <= 0) return undefined;
    return `${(-elapsed).toFixed(3)}s`;
  });
  // 底层图的动画负延迟：首图 = 覆盖层相位锁（firstPushDelay）；之后每次换底
  // 都换成「接续顶层相位」的换算值（BOTTOM_CARRY_DELAY_S），保证落底瞬间
  // 底层与顶层逐像素一致、顶层淡出完全不可见。
  const [bottomDelay, setBottomDelay] = useState(firstPushDelay);
  // 首图预加载解码完成才把整层显示出来：挂载即渲染会先透黑、图到了再突然
  // 出现，加上覆盖层同步被隐藏——两次硬切就是闪烁。
  const [ready, setReady] = useState(false);
  // 轮换清单走 ref：effect 只挂载时起一份循环，不随渲染重启（见时序模型）
  const urlsRef = useRef<string[]>(
    images.map((img) => img.fullUrl).slice(0, 5),
  );
  const indexRef = useRef(0);
  // 顶层图的引用：换底瞬间读它的动画真实进度，给新底层算接续负延迟
  const topImgRef = useRef<HTMLImageElement | null>(null);

  useEffect(() => {
    if (!bottom) return;
    let cancelled = false;
    load(bottom).then((ok) => {
      if (!cancelled && ok) setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [bottom]);

  useEffect(() => {
    if (urlsRef.current.length < 2) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let cancelled = false;

    const cycle = async () => {
      await sleep(ROTATE_INTERVAL_MS);
      // 首轮换跳过与首帧相同的照片：清单第一张通常就是主 backdrop 的另一个
      // 尺寸，快速推镜下同一张照片以 ~1.06 溶解回 1.0 的自己，特征错位读成
      // 「画面收缩 + 重影」（2026-09-21 实测反馈）；不同照片之间的同款溶解
      // 才是正常的 Ken Burns 换图节奏。
      const initialKey = initialUrl ? imageKey(initialUrl) : null;
      if (initialKey) {
        while (
          !cancelled &&
          indexRef.current < urlsRef.current.length &&
          imageKey(urlsRef.current[indexRef.current]) === initialKey
        ) {
          indexRef.current += 1;
        }
      }
      while (!cancelled) {
        if (!document.hidden) {
          const urls = urlsRef.current;
          const url = urls[indexRef.current % urls.length];
          indexRef.current += 1;
          const ok = await load(url);
          if (cancelled) return;
          if (ok) {
            setTop(url);
            setTopOn(false);
            // 等 60ms 让浏览器把新 src（opacity-0）提交后再触发过渡，否则淡入
            // 会被吞掉。不能用 requestAnimationFrame：宿主面板被遮挡/后台时
            // rAF 被节流甚至暂停，await 会永远挂起，轮换整个卡死（实测）。
            await sleep(60);
            if (cancelled) return;
            setTopOn(true);
            await sleep(FADE_MS + 100);
            if (cancelled) return;
            // 顶层已完全不透明：把同一张图落到底层、顶层摘掉，回到初始态。
            // 底层元素随 key 换图而新建、动画从零起算，会先冻在 scale 1.0，
            // 顶层淡出露出的冻结帧读成一次「回缩」——所以换底前先读顶层动画
            // 此刻的真实进度（currentTime 含起步静止期，减掉即已推时长），换算
            // 成新底层的负延迟，落底瞬间两层逐像素一致。读不到动画才退回
            // BOTTOM_CARRY_DELAY_S 兜底换算。
            const topAnim = topImgRef.current
              ?.getAnimations()
              .find((a) => (a as CSSAnimation).animationName === "nf-kenburns");
            let progressMs = BOTTOM_CARRY_DELAY_S * 1000;
            const cur = topAnim?.currentTime;
            if (typeof cur === "number") progressMs = cur - KB_START_DELAY_MS;
            setBottomDelay(`-${(Math.max(0, progressMs) / 1000).toFixed(3)}s`);
            setBottom(url);
            setTopOn(false);
            await sleep(FADE_MS + 100);
            if (cancelled) return;
            setTop(null);
          }
        } else {
          indexRef.current += 1;
        }
        await sleep(ROTATE_INTERVAL_MS);
      }
    };
    void cycle();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!bottom || typeof document === "undefined") return null;
  return createPortal(
    <div
      aria-hidden="true"
      data-ready={ready}
      className="detail-slideshow pointer-events-none fixed inset-0 [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {/* key=src（加层前缀）：换图即新元素，Ken Burns 动画从头起播。
          前缀必须有——溶解收尾时 bottom 会短暂与 top 同 URL，裸 URL 作 key
          会触发 React 重复 key 报错（每次轮换一条）。
          首图挂覆盖层相位锁定的负延迟；此后每次换底挂接续顶层相位的换算
          负延迟（见 BOTTOM_CARRY_DELAY_S），落底瞬间与顶层逐像素一致。 */}
      <img
        key={`b:${bottom}`}
        src={bottom}
        alt=""
        draggable={false}
        style={bottomDelay ? { animationDelay: bottomDelay } : undefined}
        className="absolute inset-0 size-full object-cover object-top"
      />
      {top && (
        <img
          key={`t:${top}`}
          ref={topImgRef}
          src={top}
          alt=""
          draggable={false}
          className={`absolute inset-0 size-full object-cover object-top ${
            topOn ? "opacity-100" : "opacity-0"
          }`}
        />
      )}
    </div>,
    document.body,
  );
}
