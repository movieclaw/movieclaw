"use client";

import { useCallback, useEffect, useState } from "react";

import { CheckIcon } from "@/components/icons";
import {
  type RemoteTranscodeConfigView,
  type RemoteTranscodeStatus,
  buildPairingCode,
  generateWorkerToken,
  getRemoteTranscodeConfig,
  getRemoteTranscodeStatus,
  saveRemoteTranscodeConfig,
} from "@/lib/api/transcode-worker";

const BYTES_PER_MIB = 1024 * 1024;
/** Worker 在线状态的轮询间隔；配这个页面时用户就盯着它看，要够跟手。 */
const STATUS_POLL_MS = 5000;
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
  const [status, setStatus] = useState<RemoteTranscodeStatus | null>(null);
  // 只在本次会话里持有：用于拼配对码。刷新页面即消失，服务端也读不回来。
  const [plainToken, setPlainToken] = useState<string | null>(null);
  const [pairingVisible, setPairingVisible] = useState(false);
  const [copied, setCopied] = useState(false);
  /** 当前草稿是不是刚生成的：生成的要明文给用户核对，手输的仍然遮蔽。 */
  const [tokenGenerated, setTokenGenerated] = useState(false);

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
      setTokenGenerated(false);
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

  // Worker 在线状态独立轮询：它和配置是两回事，配置保存成功不代表 Mac 连上了。
  // 拉取失败不弹错——这是个附属指示器，不该盖掉用户正在填的表单。
  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const next = await getRemoteTranscodeStatus();
        if (alive) setStatus(next);
      } catch {
        if (alive) setStatus(null);
      }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), STATUS_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

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
      // 保存成功才认这个明文——配对码里的 Token 必须和服务端存的一致
      if (clearToken) {
        setPlainToken(null);
        setPairingVisible(false);
      } else if (tokenDraft.trim()) {
        setPlainToken(tokenDraft.trim());
      }
      setTokenDraft("");
      setTokenGenerated(false);
      setClearToken(false);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2200);
      void getRemoteTranscodeStatus().then(setStatus).catch(() => {});
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

  const onlineWorkers = status?.workers.filter((w) => w.online) ?? [];
  // 配置填全了 ≠ Worker 连上了。这两件事分开说，用户才知道下一步该干什么：
  // 前者不满足要继续填表单，后者不满足要去 Mac 上看 App。
  const statusText = !config.enabled
    ? "已关闭"
    : !config.ready
      ? "配置不完整，暂不会分配远程转码任务"
      : onlineWorkers.length > 0
        ? `已就绪，${onlineWorkers.length} 个 Worker 在线`
        : "配置已就绪，但还没有 Worker 连上来";
  const statusClass = !config.enabled
    ? "text-[var(--text-muted)]"
    : config.ready && onlineWorkers.length > 0
      ? "text-emerald-300"
      : "text-amber-200";
  const pairingCode =
    plainToken && config.base_url ? buildPairingCode(config.base_url, plainToken) : null;

  function applyGeneratedToken() {
    const next = generateWorkerToken();
    setTokenDraft(next);
    setTokenGenerated(true);
    setClearToken(false);
    setError(null);
  }

  async function copyPairingCode() {
    if (!pairingCode) return;
    try {
      await navigator.clipboard.writeText(pairingCode);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("浏览器拒绝了剪贴板访问，请手动选中配对码复制");
    }
  }

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

          {/* 已连接的 Worker。配完之后「成没成」全靠这一块回答，因此即使没有
              Worker 也要显式说明，而不是把区域整个藏起来让人以为没这功能。 */}
          {config.enabled && config.ready && (
            <div className="border-t border-white/[0.06] pt-3">
              {status == null ? (
                <p className="text-caption text-[var(--text-faint)]">正在获取 Worker 状态…</p>
              ) : status.workers.length === 0 ? (
                <div className="space-y-1.5 text-caption leading-5 text-[var(--text-faint)]">
                  <p>还没有 Worker 连接。请在 Mac 上打开 MovieClaw Transcoder，</p>
                  <p>用下方的配对码填好设置后点「保存并连接」。</p>
                </div>
              ) : (
                <ul className="space-y-2">
                  {status.workers.map((worker) => (
                    <li
                      key={worker.worker_id}
                      className="rounded-xl border border-white/[0.06] bg-black/15 px-3 py-2"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sub font-medium text-[var(--text)]">
                          {worker.worker_id}
                        </span>
                        <span
                          className={
                            worker.online
                              ? worker.draining
                                ? "text-caption text-amber-200"
                                : "text-caption text-emerald-300"
                              : "text-caption text-[var(--text-faint)]"
                          }
                        >
                          {worker.online ? (worker.draining ? "暂停接单" : "在线") : "已离线"}
                        </span>
                      </div>
                      <p className="mt-1 text-caption leading-5 text-[var(--text-faint)]">
                        {[
                          worker.platform,
                          worker.arch,
                          worker.ffmpeg_version ? `ffmpeg ${worker.ffmpeg_version}` : null,
                          worker.backends.length > 0 ? worker.backends.join("/") : null,
                          `任务 ${worker.active_jobs}/${worker.max_jobs}`,
                          `${Math.round(worker.last_seen_seconds)} 秒前活跃`,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
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
              <div className="flex items-center gap-3">
                {/* 文档里写「设置一个高熵 Token」而页面上没有生成入口，实际
                    结果一定是有人填 12345678。给个按钮比给个说明有效。 */}
                <button
                  type="button"
                  onClick={applyGeneratedToken}
                  className="text-caption text-[var(--accent)] underline decoration-dotted underline-offset-2"
                >
                  生成随机令牌
                </button>
                {config.worker_token_configured && !clearToken && (
                  <button
                    type="button"
                    onClick={() => {
                      setTokenDraft("");
                      setTokenGenerated(false);
                      setClearToken(true);
                    }}
                    className="text-caption text-red-300/90 underline decoration-dotted underline-offset-2 hover:text-red-200"
                  >
                    清除令牌
                  </button>
                )}
              </div>
            </div>
            <input
              id="remote-worker-token"
              // 刚生成的令牌要能被看见和核对；用户手输时仍然遮蔽
              type={tokenGenerated ? "text" : "password"}
              value={tokenDraft}
              onChange={(event) => {
                setTokenDraft(event.target.value);
                setTokenGenerated(false);
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

      {/* 配对码：把地址和 Token 一次性交给 Mac，替掉两端手工抄写。
          只在本次会话刚保存过 Token 时可用——服务端读不回明文，这既是安全
          约束，也让「配对码 = 刚设好的这一份」语义保持清晰。 */}
      {pairingCode && (
        <section>
          <h3 className="group-label mb-2.5 px-1">配对 Worker</h3>
          <div className="css-glass space-y-3 !rounded-2xl p-5 max-sm:p-4">
            <p className="text-sub leading-relaxed text-[var(--text-muted)]">
              在 Mac 上打开 MovieClaw Transcoder 的「设置」，把下面这串粘贴到「配对码」
              一栏点「填入」，地址和令牌会自动填好。
            </p>
            {pairingVisible ? (
              <textarea
                readOnly
                value={pairingCode}
                rows={3}
                onFocus={(event) => event.target.select()}
                className={`${INPUT_CLASS} resize-none break-all font-mono text-caption`}
              />
            ) : (
              <p className="text-caption text-[var(--text-faint)]">
                配对码包含令牌明文，等同于凭据本身，请只在信任的屏幕上显示。
              </p>
            )}
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setPairingVisible((value) => !value)}
                className="btn-glass rounded-full px-4 py-1.5 text-sub"
              >
                {pairingVisible ? "隐藏" : "显示配对码"}
              </button>
              <button
                type="button"
                onClick={() => void copyPairingCode()}
                className="btn-glass rounded-full px-4 py-1.5 text-sub"
              >
                复制
              </button>
              {copied && (
                <span className="flex items-center gap-1 text-sub text-emerald-300">
                  <CheckIcon className="size-4" /> 已复制
                </span>
              )}
            </div>
          </div>
        </section>
      )}

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
