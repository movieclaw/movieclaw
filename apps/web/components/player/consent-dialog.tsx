"use client";

import Link from "next/link";
import { useState } from "react";

import { savePlaybackPolicy } from "@/lib/api/playback";
import type { PlaybackDecision } from "@/lib/api/playback";

/**
 * 软件转码同意弹窗（docs/design/web-player.md §3.6）。
 *
 * 软件转码默认关闭：低配 NAS 上一路 1080p 软转就能吃满 CPU，连带拖慢搜索、
 * 扫描、订阅——用户感知到的是「整个应用变卡了」，却完全不会联想到是自己点了
 * 播放。但默认关了也不能就此了事，那等于把「这片放不了」的死路甩给用户，
 * 所以在这里问一次、一次同意永久保存。
 *
 * 两条必须守住的：
 * - **保存粒度是全局开关**，没有「仅本次允许」的临时态——那既复杂又没价值，
 *   用户第二次遇到还得再点一遍。
 * - **普通成员看到的是说明而不是按钮**。全局设置只有超管能改，给一个点了必然
 *   403 的按钮比不给更糟。
 */
export function ConsentDialog({
  decision,
  onGranted,
  onCancel,
}: {
  decision: PlaybackDecision;
  /** 开关已翻开，可以重新决策了 */
  onGranted: () => void;
  onCancel: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canSelfEnable = decision.can_self_enable === true;

  const enable = async () => {
    setSaving(true);
    setError(null);
    try {
      const saved = await savePlaybackPolicy({ software_transcode_enabled: true });
      // 保存接口回显的就是落库后的取值：不是 true 说明开关根本没生效，此时
      // 绝不能 onGranted——重新决策还会弹回同一个窗，用户只会觉得「点了没
      // 反应」。把失败明确说出来，让人知道该去查什么。
      if (!saved.software_transcode_enabled) {
        throw new Error("软件转码开关保存后未生效，请刷新页面重试或查看服务端日志");
      }
      onGranted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "开启失败，请稍后重试");
      setSaving(false);
    }
  };

  return (
    // role=dialog 兼具语义与 media-controller 的自动淡出豁免（它的隐藏规则
    // 明确跳过 [role=dialog]）——等用户拍板的弹窗绝不能自己隐身。
    //
    // pointer-events-auto 必须显式写（2026-08-26 用户反馈「确认按钮点着没反应」
    // 的根因）：media-chrome 的浮层容器是 pointer-events:none，靠
    // `::slotted(...)` 规则给子元素恢复命中，但那条规则**同样明确跳过
    // [role=dialog]**（media-container 的样式表里两处豁免是同一个选择器）。
    // 不自己声明的话整个弹窗继承 none——看得见、点不中，点击穿透到画面上。
    <div
      role="dialog"
      aria-modal="true"
      className="pointer-events-auto absolute inset-0 z-30 flex items-center justify-center bg-black/75 px-6"
    >
      {/* 面板外观照抄站内 Modal（components/modal.tsx）：同一个圆角、描边、
          底色与投影——它就是一个模态，不该长得像另一套系统 */}
      <div className="w-full max-w-[460px] rounded-2xl border border-white/10 bg-[rgba(16,18,26,0.92)] p-7 text-white shadow-[0_32px_90px_rgba(0,0,0,0.7)] backdrop-blur-2xl">
        <h2 className="text-[19px] font-semibold leading-snug">这部片需要软件转码才能在浏览器里播放</h2>

        <dl className="mt-4 space-y-3 text-[13px] leading-relaxed text-white/70">
          <div>
            <dt className="text-white/45">原因</dt>
            <dd className="mt-0.5 text-white/85">{decision.reason}</dd>
          </div>
          {decision.cost_hint ? (
            <div>
              <dt className="text-white/45">代价</dt>
              <dd className="mt-0.5 text-white/85">{decision.cost_hint}</dd>
            </div>
          ) : null}
        </dl>

        {error ? (
          <p className="mt-4 rounded-xl bg-[var(--danger)]/20 px-3 py-2 text-[13px] text-white">{error}</p>
        ) : null}

        {canSelfEnable ? (
          <>
            {/* 用户看到这个弹窗的一刻，正是他最需要远程硬件转码的时候——NAS 没有
                可用硬件编码器，只能拿 CPU 硬扛。这个功能藏在设置的「播放」分区里，
                不在这里说一句就几乎没人会发现。只对能改全局设置的管理员显示：给成员
                一个点进去必然 403 的链接毫无意义。 */}
            <p className="mt-4 rounded-xl bg-white/[0.06] px-3 py-2.5 text-[12px] leading-relaxed text-white/60">
              局域网里有 Apple Silicon Mac 的话，可以让它替 NAS 做硬件转码，省下这里的
              CPU 开销。
              <Link
                href={{ pathname: "/settings/playback" }}
                className="ml-1 text-[var(--player-accent)] underline decoration-dotted underline-offset-2"
              >
                去设置远程转码
              </Link>
            </p>
            {/* 独立播放设置页已撤（2026-08-25），这里如实告知开启即长期生效 */}
            <p className="mt-3 text-[12px] text-white/45">开启后长期生效，之后不再询问。</p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={onCancel}
                className="rounded-full px-5 py-2.5 text-[14px] text-white/70 transition-colors hover:bg-white/10"
              >
                取消
              </button>
              <button
                type="button"
                onClick={enable}
                disabled={saving}
                className="rounded-full bg-[var(--player-accent)] px-5 py-2.5 text-[14px] font-semibold text-black transition-colors hover:bg-[var(--player-accent-hover)] disabled:opacity-50"
              >
                {saving ? "正在开启…" : "开启并播放"}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="mt-4 text-[13px] leading-relaxed text-white/70">
              当前未开启软件转码。请联系管理员开启（管理员播放此类影片时会收到开启询问）。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={onCancel}
                className="rounded-full bg-white/15 px-5 py-2.5 text-[14px] text-white transition-colors hover:bg-white/25"
              >
                知道了
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
