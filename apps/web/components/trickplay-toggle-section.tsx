"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchPlaybackPolicy, savePlaybackPolicy } from "@/lib/api/playback";

/**
 * 「播放」分区：进度条预览缩略图的生成开关。
 *
 * 预览图要通读整部影片抽帧（4 倍速限速下两小时片约 30 分钟），低配 NAS 上
 * 是一段可感知的 CPU/磁盘负载——它默认打开（预览是「这个播放器不便宜」的
 * 第一印象来源），但必须给用户一个体面的关法，而不是逼他去翻 compose 环境变量。
 *
 * 与远程转码那张「改动后按保存」的表单不同，这里只有一个开关，改成即存、
 * 失败即回滚并提示，不需要单独的保存按钮。已生成的预览不受开关影响：
 * 关掉只是不再生成新的，重新打开即恢复。
 */
export function TrickplayToggleSection() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchPlaybackPolicy()
      .then((policy) => {
        if (alive) setEnabled(policy.trickplay_enabled);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  const toggle = useCallback(
    async (next: boolean) => {
      const previous = enabled;
      setEnabled(next); // 乐观更新：失败回滚，成功时省一次等待
      setBusy(true);
      setError(null);
      try {
        const policy = await savePlaybackPolicy({ trickplay_enabled: next });
        setEnabled(policy.trickplay_enabled);
      } catch (e) {
        setEnabled(previous);
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [enabled],
  );

  return (
    <section>
      <h3 className="group-label mb-2.5 px-1">进度条预览</h3>
      <div className="css-glass space-y-4 !rounded-2xl p-5 max-sm:p-4">
        {error && (
          <div
            role="alert"
            className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff9b9b]"
          >
            {error}
          </div>
        )}
        <label className="flex cursor-pointer items-center justify-between gap-4">
          <span>
            <span className="block text-body font-medium text-[var(--text)]">
              生成进度条预览图
            </span>
            <span className="mt-0.5 block text-caption leading-5 text-[var(--text-faint)]">
              {enabled
                ? "拖动进度条时可以看到画面缩略图；影片入库后首次播放会在后台慢慢生成"
                : "不再为新影片生成预览，已生成的照常显示；重新打开即恢复生成"}
            </span>
          </span>
          {enabled !== null && (
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy}
              onChange={(event) => void toggle(event.target.checked)}
              className="size-5 shrink-0 accent-[var(--accent)]"
            />
          )}
        </label>
      </div>
    </section>
  );
}
