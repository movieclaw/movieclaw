"use client";

import { useCallback, useEffect, useState } from "react";

import { CheckIcon } from "@/components/icons";
import {
  type RemoteTranscodeConfigView,
  getRemoteTranscodeConfig,
  saveRemoteTranscodeConfig,
} from "@/lib/api/transcode-worker";

const BYTES_PER_MIB = 1024 * 1024;
const MAX_ARTIFACT_MIB = 512;
const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-sub " +
  "text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
  "focus:border-[var(--accent)]/50";

export interface RemoteTranscodeSectionProps {
  /** 系统外部访问地址未配置且专用地址为空时，切回「网络与维护」Tab。 */
  onOpenMaintain?: () => void;
}

/**
 * 「应用 → 远程转码」设置。
 *
 * 远程转码可以使用专用的 HTTP(S) 入口；专用地址留空时跟随系统「网络与维护」
 * 中的外部访问地址。源文件 URL、HLS 上传 URL 和 Worker 控制地址始终使用同一
 * 个有效入口，避免三类请求走到不同的主机或端口。
 */
export function RemoteTranscodeSection({ onOpenMaintain }: RemoteTranscodeSectionProps) {
  const [config, setConfig] = useState<RemoteTranscodeConfigView | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [baseURLDraft, setBaseURLDraft] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [maxArtifactMiB, setMaxArtifactMiB] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getRemoteTranscodeConfig();
      setConfig(next);
      setEnabled(next.enabled);
      setBaseURLDraft(next.base_url_override);
      setMaxArtifactMiB(String(Math.max(1, Math.round(next.max_artifact_bytes / BYTES_PER_MIB))));
      setTokenDraft("");
      setClearToken(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    const mib = Number(maxArtifactMiB.trim());
    if (!Number.isSafeInteger(mib) || mib <= 0) {
      setError("分片大小必须是大于 0 的整数 MiB");
      return;
    }
    if (mib > MAX_ARTIFACT_MIB) {
      setError(`分片大小不能超过 ${MAX_ARTIFACT_MIB} MiB`);
      return;
    }
    if (mib > Math.floor(Number.MAX_SAFE_INTEGER / BYTES_PER_MIB)) {
      setError("分片大小超出允许范围");
      return;
    }

    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const next = await saveRemoteTranscodeConfig({
        enabled,
        base_url: baseURLDraft.trim(),
        worker_token: clearToken ? "" : tokenDraft.trim() || null,
        max_artifact_bytes: mib * BYTES_PER_MIB,
      });
      setConfig(next);
      setEnabled(next.enabled);
      setBaseURLDraft(next.base_url_override);
      setMaxArtifactMiB(String(Math.max(1, Math.round(next.max_artifact_bytes / BYTES_PER_MIB))));
      setTokenDraft("");
      setClearToken(false);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2200);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载远程转码设置…</p>;
  }

  if (config == null) {
    return (
      <div className="space-y-3">
        <p className="text-ui text-[var(--text-muted)]">远程转码设置加载失败</p>
        <button type="button" onClick={() => void load()} className="btn-glass px-3 py-1.5 text-sub">
          重试
        </button>
        {error && <p className="text-sub text-red-300">{error}</p>}
      </div>
    );
  }

  const statusText = !config.enabled
    ? "已关闭"
    : config.ready
      ? "配置已就绪"
      : "配置不完整，暂不会分配远程转码任务";
  const statusClass = !config.enabled
    ? "text-[var(--text-muted)]"
    : config.ready
      ? "text-emerald-300"
      : "text-amber-200";

  return (
    <div className="space-y-7">
      {error && (
        <div
          role="alert"
          className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff9b9b]"
        >
          {error}
        </div>
      )}

      <p className="text-sub leading-relaxed text-[var(--text-muted)]">
        只把需要远程硬件能力的转码任务交给兼容 Worker。NAS 仍负责鉴权、播放会话和 HLS
        缓存；修改后立即生效，不需要重启应用。当前可用的 Worker 实现为 macOS Apple
        Silicon 版本。
      </p>

      <section>
        <h3 className="group-label mb-2.5 px-1">运行状态</h3>
        <div className="css-glass space-y-4 !rounded-2xl p-5 max-sm:p-4">
          <label className="flex cursor-pointer items-center justify-between gap-4">
            <span>
              <span className="block text-body font-medium text-[var(--text)]">启用远程硬件转码</span>
              <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                关闭时所有播放任务继续使用 NAS 本地转码路径
              </span>
            </span>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
              className="size-5 shrink-0 accent-[var(--accent)]"
            />
          </label>
          <div className="border-t border-white/[0.06] pt-3 text-sub">
            当前状态：<span className={statusClass}>{statusText}</span>
          </div>
          {config.issues.length > 0 && (
            <ul className="space-y-1 text-caption text-amber-200/90">
              {config.issues.map((issue) => <li key={issue}>· {issue}</li>)}
            </ul>
          )}
        </div>
      </section>

      <section>
        <h3 className="group-label mb-2.5 px-1">连接与安全</h3>
        <div className="css-glass space-y-5 !rounded-2xl p-5 max-sm:p-4">
          <div>
            <div className="flex items-center justify-between gap-3">
              <label htmlFor="remote-worker-token" className="text-body font-medium text-[var(--text)]">
                Worker Token
              </label>
              {config.worker_token_configured && !clearToken && (
                <button
                  type="button"
                  onClick={() => {
                    setTokenDraft("");
                    setClearToken(true);
                  }}
                  className="text-caption text-red-300/90 underline decoration-dotted underline-offset-2 hover:text-red-200"
                >
                  清除令牌
                </button>
              )}
            </div>
            <input
              id="remote-worker-token"
              type="password"
              value={tokenDraft}
              onChange={(event) => {
                setTokenDraft(event.target.value);
                setClearToken(false);
              }}
              placeholder={
                clearToken
                  ? "保存后清除当前令牌"
                  : config.worker_token_configured
                    ? "已配置，留空保持不变"
                    : "输入与远程 Worker 相同的高熵令牌"
              }
              autoComplete="new-password"
              className={`mt-2 ${INPUT_CLASS}`}
            />
            <p className="mt-1.5 text-caption leading-5 text-[var(--text-faint)]">
              令牌会加密保存，页面和接口都不会回显明文。修改令牌后，远程 Worker 需要同步更新。
            </p>
          </div>

          <div>
            <label htmlFor="remote-base-url" className="text-body font-medium text-[var(--text)]">
              远程转码外部访问地址（可选）
            </label>
            <input
              id="remote-base-url"
              type="url"
              value={baseURLDraft}
              onChange={(event) => setBaseURLDraft(event.target.value)}
              placeholder="留空时使用系统外部访问地址"
              className={`mt-2 ${INPUT_CLASS}`}
              spellCheck={false}
            />
            <p className="mt-1.5 text-caption leading-5 text-[var(--text-faint)]">
              填写后仅远程转码使用此地址；留空则回退到系统「网络与维护」中的外部访问地址。
              可填写可信内网的 HTTP 地址，端口请使用 NAS 实际对外映射端口。
            </p>
            <div className="mt-3 rounded-xl border border-white/[0.06] bg-black/15 px-3 py-2 text-sub">
              <span className="text-[var(--text-faint)]">当前生效地址：</span>{" "}
              <span className="font-mono text-[var(--text-muted)]">{config.base_url || "未设置"}</span>
            </div>
            <p className="mt-1.5 text-caption leading-5 text-[var(--text-faint)]">
              {config.base_url_source === "remote_transcode_setting"
                ? "当前使用远程转码专用地址。"
                : config.base_url_source === "system_external_url"
                  ? "当前使用系统外部访问地址。"
                  : "尚未设置可用的远程转码地址。"}
            </p>
            {!config.base_url && !baseURLDraft.trim() && onOpenMaintain && (
              <button
                type="button"
                onClick={onOpenMaintain}
                className="mt-2 text-caption text-[var(--accent)] underline decoration-dotted underline-offset-2"
              >
                去“网络与维护”设置外部访问地址
              </button>
            )}
          </div>
        </div>
      </section>

      <section>
        <h3 className="group-label mb-2.5 px-1">传输限制</h3>
        <div className="css-glass !rounded-2xl p-5 max-sm:p-4">
          <label htmlFor="remote-artifact-limit" className="text-body font-medium text-[var(--text)]">
            单个 HLS 产物大小上限
          </label>
          <div className="mt-2 flex max-w-[300px] items-center gap-2">
            <input
              id="remote-artifact-limit"
              type="number"
              min={1}
              max={MAX_ARTIFACT_MIB}
              step={1}
              value={maxArtifactMiB}
              onChange={(event) => setMaxArtifactMiB(event.target.value)}
              className={`min-w-0 flex-1 ${INPUT_CLASS}`}
            />
            <span className="text-sub text-[var(--text-muted)]">MiB</span>
          </div>
          <p className="mt-1.5 text-caption leading-5 text-[var(--text-faint)]">
            限制 NAS 接收的 init、playlist 和 fMP4 分片大小，默认和最大值均为 512 MiB。
          </p>
        </div>
      </section>

      <div className="flex items-center justify-end gap-3">
        {saved && (
          <span className="flex items-center gap-1 text-sub text-emerald-300">
            <CheckIcon className="size-4" /> 已保存
          </span>
        )}
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="btn-accent rounded-full px-5 py-2 text-sub font-semibold disabled:opacity-50"
        >
          {busy ? "保存中…" : "保存设置"}
        </button>
      </div>
    </div>
  );
}
