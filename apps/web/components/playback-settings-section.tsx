"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { LiquidGlassButton } from "@/vendor/liquid-glass";

import { InfoIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import { useBackdrop } from "@/lib/backdrop";
import {
  type PlaybackPolicy,
  fetchPlaybackPolicy,
  savePlaybackPolicy,
} from "@/lib/api/playback";

/**
 * 播放设置（设置 → 播放，docs/design/web-player.md §3.6 / §4.5 / §4.6）。
 *
 * 与网络设置同一套交互模型：**自动保存，立即生效**，没有保存按钮。
 *
 * 页面顶部先摆「硬件加速」这条**实测结论**而不是开关——用户改不了自己有没有
 * 显卡，把它做成开关只会让人误配，然后对着必然失败的转码找原因。软件转码
 * 开关紧随其后，因为它的意义完全由上面那条结论决定：有显卡时它几乎用不上，
 * 没显卡时它是「能不能放 HEVC」的唯一开关。
 */

/** 数值项的取值范围，与后端 PlaybackPolicySetting 的 ge/le 一致。 */
const NUMBER_FIELDS = [
  {
    key: "max_transcode_concurrency",
    label: "转码并发上限",
    min: 1,
    max: 8,
    help: "同时进行的转码会话上限，按 CPU / GPU 能力设定。超过上限的播放请求会被直接拒绝并告知当前占用，而不是排队——在 HTTP 请求里排队等于让用户对着转圈等一个不知道多久的位置。",
  },
  {
    key: "max_remux_concurrency",
    label: "直通并发上限",
    min: 1,
    max: 16,
    help: "同时进行的直通（remux / 只转音轨）会话上限。与转码分开计数：机械盘上两路 4K 直通就能打满随机读，而 CPU 几乎是空的——瓶颈在 IO 不在算力。",
  },
  {
    key: "max_transcode_height",
    label: "转码输出高度上限",
    min: 480,
    max: 2160,
    help: "需要转码时输出画面的最大高度，超出则降分辨率。4K 转码的算力开销是 1080p 的四倍以上，家用设备通常撑不住。",
  },
  {
    key: "transcode_cache_quota_gb",
    label: "分片缓存上限（GB）",
    min: 1,
    max: 500,
    help: "转码分片的占盘上限。分片与数据库同在 data/ 卷上，盘满会让 SQLite 写不进去、整个应用不可用，所以写入前会先检查剩余空间。",
  },
] as const;

type NumberField = (typeof NUMBER_FIELDS)[number]["key"];

export function PlaybackSettingsSection() {
  const { backdrop } = useBackdrop();
  const [policy, setPolicy] = useState<PlaybackPolicy | null>(null);
  const [failed, setFailed] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  // 保存串行化：连点开关时按顺序落库，避免旧请求的响应覆盖新配置
  const saveChain = useRef<Promise<unknown>>(Promise.resolve());
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    fetchPlaybackPolicy().then(setPolicy).catch(() => setFailed(true));
    return () => {
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
  }, []);

  const commit = useCallback((changes: Partial<PlaybackPolicy>) => {
    setPolicy((prev) => (prev ? { ...prev, ...changes } : prev));
    setSaveState("saving");
    setSaveError(null);
    saveChain.current = saveChain.current
      .then(() => savePlaybackPolicy(changes))
      .then((saved) => {
        setPolicy(saved);
        setSaveState("saved");
        if (savedTimer.current) clearTimeout(savedTimer.current);
        savedTimer.current = setTimeout(() => setSaveState("idle"), 1600);
      })
      .catch((err: unknown) => {
        setSaveState("error");
        setSaveError(err instanceof Error ? err.message : "保存失败");
        // 落库失败要把界面拉回服务端真值，否则开关停在一个并不存在的状态上
        fetchPlaybackPolicy().then(setPolicy).catch(() => undefined);
      });
  }, []);

  if (failed) {
    return <p className="px-1 text-body text-[var(--text-faint)]">播放设置加载失败，请稍后重试。</p>;
  }
  if (!policy) {
    return <p className="px-1 text-body text-[var(--text-faint)]">正在加载…</p>;
  }

  return (
    <div className="space-y-7">
      {saveState !== "idle" && (
        <p
          className={`px-1 text-caption ${
            saveState === "error" ? "text-red-300" : "text-[var(--text-faint)]"
          }`}
        >
          {saveState === "saving" ? "保存中…" : saveState === "saved" ? "已保存" : saveError}
        </p>
      )}

      {/* —— 转码方式 —— */}
      <section>
        <div className="mb-2.5 flex items-center gap-1.5 px-1">
          <h3 className="group-label">转码方式</h3>
          <HelpDot
            content={
              <>
                <p>网页播放优先原文件直连，放不了才转码。这里决定「放不了的时候用什么转」。</p>
                <p className="mt-1.5">
                  硬件加速是实测结果，不是开关——容器部署需要挂载 /dev/dri 或显卡设备，
                  并把容器用户加入 render 组。
                </p>
              </>
            }
          />
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          <div className="flex items-center gap-3 px-5 py-3.5">
            <span className="text-body font-medium text-[var(--text)]">硬件加速</span>
            <span className="ml-auto flex items-center gap-2 text-sub">
              <span
                className={`size-1.5 rounded-full ${
                  policy.hardware_available ? "bg-[var(--ok)]" : "bg-[var(--text-faint)]"
                }`}
              />
              <span
                className={
                  policy.hardware_available ? "text-emerald-300/90" : "text-[var(--text-faint)]"
                }
              >
                {policy.hardware_available
                  ? `可用 · ${policy.hw_backends.join(" / ").toUpperCase()}`
                  : "未检测到可用设备"}
              </span>
            </span>
          </div>

          <div className="px-5 py-3.5">
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1.5">
                <span className="text-body font-medium text-[var(--text)]">允许软件转码</span>
                <HelpDot
                  content={
                    <>
                      <p>
                        没有可用显卡时，用 CPU 转码作为兜底。默认关闭：低配 NAS 上一路 1080p
                        软转就能吃满 CPU，连带拖慢搜索、扫描、订阅——用户感知到的是「整个应用
                        变卡」，却不会联想到是自己点了播放。
                      </p>
                      <p className="mt-1.5">
                        关闭状态下遇到必须转码的片子，播放页会弹窗说明代价并询问，同意后即在此处永久打开。
                      </p>
                    </>
                  }
                />
              </span>
              <span className="ml-auto">
                <LiquidGlassButton
                  backgroundImage={backdrop}
                  variant="dark"
                  checked={policy.software_transcode_enabled}
                  aria-label="允许软件转码"
                  onCheckedChange={() =>
                    commit({ software_transcode_enabled: !policy.software_transcode_enabled })
                  }
                  className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
                >
                  <span className="sr-only">
                    {policy.software_transcode_enabled ? "已允许" : "已关闭"}
                  </span>
                </LiquidGlassButton>
              </span>
            </div>
            {!policy.hardware_available && !policy.software_transcode_enabled && (
              <p className="mt-2 text-caption text-[var(--text-faint)]">
                当前既没有硬件加速也不允许软件转码：浏览器放不了的片源（如 HEVC、
                需要色调映射的 HDR）会明确告知放不了，而不是转半天。
              </p>
            )}
          </div>
        </div>
      </section>

      {/* —— 资源上限 —— */}
      <section>
        <div className="mb-2.5 flex items-center gap-1.5 px-1">
          <h3 className="group-label">资源上限</h3>
          <HelpDot content="播放是这台机器上最吃资源的动作，这几项决定它最多能占多少，避免把订阅、扫描等后台任务挤死。" />
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {NUMBER_FIELDS.map((field) => (
            <div key={field.key} className="flex items-center gap-4 px-5 py-4">
              <span className="flex shrink-0 items-center gap-1.5">
                <span className="text-body font-medium text-[var(--text)]">{field.label}</span>
                <HelpDot content={<p>{field.help}</p>} />
              </span>
              <span className="ml-auto flex items-center gap-2">
                <input
                  type="number"
                  min={field.min}
                  max={field.max}
                  defaultValue={policy[field.key as NumberField]}
                  onBlur={(e) => {
                    const value = Number(e.target.value);
                    if (!Number.isInteger(value) || value < field.min || value > field.max) {
                      // 越界就还原成当前生效值：不让界面停在一个后端会拒绝的数字上
                      e.target.value = String(policy[field.key as NumberField]);
                      return;
                    }
                    if (value !== policy[field.key as NumberField]) {
                      commit({ [field.key]: value } as Partial<PlaybackPolicy>);
                    }
                  }}
                  onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                  className="tnum w-24 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-right text-sub text-[var(--text)] outline-none transition-colors focus:border-[var(--accent)]/50"
                />
                <span className="w-16 text-caption text-[var(--text-faint)]">
                  {field.min}–{field.max}
                </span>
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

/** ⓘ 说明按钮：与网络设置同款，说明文字一律收在 tooltip 里。 */
function HelpDot({ content }: { content: React.ReactNode }) {
  return (
    <Tooltip content={content} placement="top" maxWidth={340}>
      <button
        type="button"
        aria-label="说明"
        className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
      >
        <InfoIcon className="size-[15px]" />
      </button>
    </Tooltip>
  );
}
