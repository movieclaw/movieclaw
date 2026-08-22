"use client";

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
      await savePlaybackPolicy({ software_transcode_enabled: true });
      onGranted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "开启失败，请稍后重试");
      setSaving(false);
    }
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/70 px-6">
      <div className="w-full max-w-[440px] rounded-2xl border border-white/10 bg-[#16181d] p-6 text-white shadow-2xl">
        <h2 className="text-[17px] font-semibold">这部片需要软件转码才能在浏览器里播放</h2>

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
          <p className="mt-4 rounded-lg bg-red-500/15 px-3 py-2 text-[13px] text-red-200">{error}</p>
        ) : null}

        {canSelfEnable ? (
          <>
            <p className="mt-4 text-[12px] text-white/45">之后可在「设置 → 播放」里随时关闭。</p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={onCancel}
                className="rounded-xl px-4 py-2 text-[13px] text-white/70 transition-colors hover:bg-white/10"
              >
                取消
              </button>
              <button
                type="button"
                onClick={enable}
                disabled={saving}
                className="rounded-xl bg-white px-4 py-2 text-[13px] font-medium text-black transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "正在开启…" : "开启并播放"}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="mt-4 text-[13px] leading-relaxed text-white/70">
              当前未开启软件转码。请联系管理员在「设置 → 播放」中开启。
            </p>
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={onCancel}
                className="rounded-xl bg-white/10 px-4 py-2 text-[13px] text-white transition-colors hover:bg-white/15"
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
