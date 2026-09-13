"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchPlaybackPolicy, savePlaybackPolicy } from "@/lib/api/playback";

/**
 * 「播放」分区：转码产物复用开关（docs/design/player-pipeline-optimization.md §B）。
 *
 * 开着时会话结束不删分片，同一部片同一档位再播直接读文件、不重转——续播
 * 首帧从「起 ffmpeg 转首片」变成读文件。代价是盘上常驻一块缓存（按剩余空间
 * 四分之一自动限额、24 小时未用自动清理，存储页可一键清空）。盘特别紧的
 * 部署要能关掉它，所以给一个开关而不是只写在文档里。
 *
 * 交互与进度条预览那颗开关同款：改成即存、失败回滚。
 */
export function TranscodeCacheToggleSection() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchPlaybackPolicy()
      .then((policy) => {
        if (alive) setEnabled(policy.transcode_cache_enabled);
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
      setEnabled(next);
      setBusy(true);
      setError(null);
      try {
        const policy = await savePlaybackPolicy({ transcode_cache_enabled: next });
        setEnabled(policy.transcode_cache_enabled);
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
      <h3 className="group-label mb-2.5 px-1">转码缓存</h3>
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
              保留转码产物供续播、重看复用
            </span>
            <span className="mt-0.5 block text-caption leading-5 text-[var(--text-faint)]">
              {enabled
                ? "同一部片再次播放时已转出的部分直接读文件、不重新转码；缓存按磁盘剩余空间自动限额、24 小时未用自动清理，也可在「存储」页清空"
                : "会话结束即删除分片，每次播放都重新转码；磁盘特别紧张时用"}
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
