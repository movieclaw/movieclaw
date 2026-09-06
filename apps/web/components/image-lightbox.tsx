"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, XIcon } from "@/components/icons";

/**
 * 多图灯箱：全屏浏览一组图片（海报 + 截图等）。
 *
 * 交互对齐主流图片预览站：
 *   - 大图居中，左右按钮 / ←→ 方向键切换，Esc 或点击空白处关闭；
 *   - 底部缩略图条（当前项高亮环），点击直达；
 *   - 顶部计数「n / 总数」+ 标题 + 关闭按钮；
 *   - 相邻图片预加载，切换不白屏。
 *
 * 通用组件：只吃 images 数组，与搜索无耦合，未来详情页/订阅页可直接复用。
 * Portal 到 body：避免被玻璃面板的层叠上下文（isolation:isolate）困住。
 * PT 站图片多为外链图床，统一 referrerPolicy=no-referrer 绕过防盗链；
 * 单张加载失败显示占位文案，不影响浏览其余图片。
 */
/** 灯箱顶栏的可选异步操作（如「设为背景」）：状态机与反馈由灯箱统一负责。 */
export interface LightboxAction {
  /** 常态文案，如「设为背景」 */
  label: string;
  /** 执行中的文案，如「正在设置…」 */
  busyLabel: string;
  /** 成功后的文案，如「已设为背景」 */
  doneLabel: string;
  /** 按钮图标（常态与成功态共用外形，成功态换为对勾） */
  icon?: React.ReactNode;
  /** 对当前第 index 张图执行动作；reject 的 message 会展示给用户 */
  run: (index: number) => Promise<void>;
}

export interface ImageLightboxProps {
  /** 要浏览的图片地址（按展示顺序） */
  images: string[];
  /** 初始展示的下标 */
  initialIndex?: number;
  /** 顶部标题（如种子名），可省略 */
  title?: string;
  /** 逐张说明（如章节「标题 · 12:30」）；有则顶栏按当前张显示，优先于 title */
  captions?: string[];
  /** 顶栏操作按钮（可省略）；耗时操作执行期间按钮转菊花并禁用，不阻断继续浏览 */
  action?: LightboxAction;
  /** 底部缩略图形状：竖版 2:3（海报/截图，默认）或宽幅 16:9（剧照） */
  thumbAspect?: "portrait" | "landscape";
  onClose: () => void;
}

export function ImageLightbox({
  images,
  initialIndex = 0,
  title,
  captions,
  action,
  thumbAspect = "portrait",
  onClose,
}: ImageLightboxProps) {
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(initialIndex, 0), images.length - 1),
  );
  // 加载失败的图片下标集合：显示占位而非破图图标
  const [broken, setBroken] = useState<Set<number>>(new Set());

  // 顶栏操作的状态机：idle → busy →（done | idle+错误提示）。
  // busy 期间按钮禁用（防止重复触发同一耗时操作），但不阻断继续翻图浏览；
  // done / 错误 都是针对触发时那张图的反馈，翻到别的图即复位。
  const [actionStatus, setActionStatus] = useState<"idle" | "busy" | "done">("idle");
  const [actionError, setActionError] = useState<string | null>(null);

  const runAction = useCallback(async () => {
    if (!action || actionStatus === "busy") return;
    const target = index; // 锁定触发时的图：执行期间翻图不影响作用对象
    setActionError(null);
    setActionStatus("busy");
    try {
      await action.run(target);
      setActionStatus("done");
    } catch (err) {
      setActionStatus("idle");
      setActionError(err instanceof Error && err.message ? err.message : "操作失败，请重试");
    }
  }, [action, actionStatus, index]);

  const step = useCallback(
    (delta: number) =>
      setIndex((prev) => (prev + delta + images.length) % images.length),
    [images.length],
  );

  // 翻图后清掉上一张的成功/失败反馈（进行中的操作不受影响，继续后台执行）
  useEffect(() => {
    setActionError(null);
    setActionStatus((prev) => (prev === "busy" ? prev : "idle"));
  }, [index]);

  // 键盘：Esc 关闭，←/→ 切换
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, step]);

  // 打开期间锁住页面滚动（滚轮不应滚动身后的结果列表）
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // 预加载相邻图片：切换时不白屏
  useEffect(() => {
    for (const neighbor of [index - 1, index + 1]) {
      const url = images[(neighbor + images.length) % images.length];
      if (url) {
        const img = new Image();
        img.referrerPolicy = "no-referrer";
        img.src = url;
      }
    }
  }, [index, images]);

  const markBroken = (i: number) =>
    setBroken((prev) => (prev.has(i) ? prev : new Set(prev).add(i)));

  if (images.length === 0) return null;
  const heading = captions?.[index] ?? title;

  return createPortal(
    // 点击空白处关闭；内容区各元素自行 stopPropagation。
    // bottom 越出视口 --vp-overshoot：黑底铺到屏幕物理底边（iOS 独立 App
    // 的视口矮一截，见 globals.css）；底部缩略图条用加大的 pb 留在视口内
    <div
      role="dialog"
      aria-modal="true"
      aria-label={heading ? `图片浏览：${heading}` : "图片浏览"}
      onClick={onClose}
      className="fixed inset-0 z-[70] flex flex-col bg-black/85 backdrop-blur-md [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {/* 顶栏：计数 + 标题 + 关闭 */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 px-4 py-3 text-white/85 [padding-top:calc(0.75rem+var(--safe-top))] max-md:gap-2 max-md:px-3">
        <span className="tnum shrink-0 rounded-full bg-white/[0.1] px-2.5 py-0.5 text-sub">
          {index + 1} / {images.length}
        </span>
        {heading && <p className="min-w-0 flex-1 truncate text-ui text-white/70">{heading}</p>}

        {/* 可选操作按钮（如「设为背景」）：busy 转菊花禁用，done 变对勾，错误就地提示 */}
        {action && (
          <div className="flex min-w-0 shrink-0 items-center gap-2">
            {actionError && (
              <span className="max-w-[260px] truncate text-sub text-[#f2a4a4]" title={actionError}>
                {actionError}
              </span>
            )}
            <button
              type="button"
              disabled={actionStatus !== "idle"}
              onClick={(e) => {
                e.stopPropagation();
                void runAction();
              }}
              className={`flex h-8 items-center gap-1.5 rounded-full px-3.5 text-sub font-medium transition-colors ${
                actionStatus === "done"
                  ? "bg-white/[0.1] text-white/85"
                  : "bg-white/[0.12] text-white hover:bg-white/[0.2] disabled:cursor-default disabled:hover:bg-white/[0.12]"
              }`}
            >
              {actionStatus === "busy" ? (
                <svg
                  viewBox="0 0 24 24"
                  className="size-3.5 animate-spin"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <path d="M12 3a9 9 0 1 1-9 9" />
                </svg>
              ) : actionStatus === "done" ? (
                <CheckIcon className="size-3.5 text-[var(--ok)]" />
              ) : (
                action.icon
              )}
              {actionStatus === "busy"
                ? action.busyLabel
                : actionStatus === "done"
                  ? action.doneLabel
                  : action.label}
            </button>
          </div>
        )}

        <button
          type="button"
          aria-label="关闭图片浏览"
          onClick={(e) => {
            e.stopPropagation();
            onClose();
          }}
          className="ml-auto shrink-0 rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white"
        >
          <XIcon className="size-5" />
        </button>
      </div>

      {/* 主体：大图 + 两侧切换按钮 */}
      <div className="relative flex min-h-0 flex-1 items-center justify-center px-14 max-md:px-2">
        {broken.has(index) ? (
          <div
            onClick={(e) => e.stopPropagation()}
            className="rounded-2xl border border-white/[0.12] bg-white/[0.04] px-8 py-10 text-center text-ui text-white/60"
          >
            这张图片加载失败
            <span className="mt-1 block text-caption text-white/40">
              图床可能已失效或拒绝外链访问
            </span>
          </div>
        ) : (
          <img
            key={images[index]}
            src={images[index]}
            alt={`第 ${index + 1} 张图片`}
            referrerPolicy="no-referrer"
            onError={() => markBroken(index)}
            onClick={(e) => e.stopPropagation()}
            className="max-h-full max-w-full select-none rounded-lg object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)]"
          />
        )}

        {images.length > 1 && (
          <>
            <button
              type="button"
              aria-label="上一张"
              onClick={(e) => {
                e.stopPropagation();
                step(-1);
              }}
              className="absolute left-3 top-1/2 -translate-y-1/2 rounded-full bg-white/[0.08] p-2.5 text-white/80 backdrop-blur transition-colors hover:bg-white/[0.18] hover:text-white"
            >
              <ChevronLeftIcon className="size-6" />
            </button>
            <button
              type="button"
              aria-label="下一张"
              onClick={(e) => {
                e.stopPropagation();
                step(1);
              }}
              className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full bg-white/[0.08] p-2.5 text-white/80 backdrop-blur transition-colors hover:bg-white/[0.18] hover:text-white"
            >
              <ChevronRightIcon className="size-6" />
            </button>
          </>
        )}
      </div>

      {/* 底部缩略图条：多图时展示，当前项高亮环 */}
      {images.length > 1 && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="scroll-none shrink-0 overflow-x-auto px-4 py-3 [padding-bottom:calc(0.75rem+var(--safe-bottom)+var(--vp-overshoot))] max-md:px-3"
        >
          <div className="mx-auto flex w-max gap-2">
            {images.map((url, i) => (
              <button
                key={`${i}:${url}`}
                type="button"
                aria-label={`查看第 ${i + 1} 张图片`}
                aria-current={i === index}
                onClick={() => setIndex(i)}
                className={`h-14 shrink-0 overflow-hidden rounded-md transition ${
                  thumbAspect === "landscape" ? "w-[100px]" : "w-10"
                } ${
                  i === index
                    ? "ring-2 ring-[var(--accent)]"
                    : "opacity-55 hover:opacity-90"
                }`}
              >
                <img
                  src={url}
                  alt=""
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  onError={() => markBroken(i)}
                  className="h-full w-full bg-white/[0.05] object-cover"
                />
              </button>
            ))}
          </div>
        </div>
      )}
    </div>,
    document.body,
  );
}
