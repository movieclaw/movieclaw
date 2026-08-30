"use client";

import { useCallback, useEffect, useState } from "react";

import { CheckIcon } from "@/components/icons";
import {
  type DeviceRequestView,
  type DeviceTokenView,
  listDeviceRequests,
  listDevices,
} from "@/lib/api/devices";
import { relativeTime } from "@/lib/devices-display";
import {
  type RemoteTranscodeConfigView,
  type RemoteTranscodeStatus,
  getRemoteTranscodeConfig,
  getRemoteTranscodeStatus,
  saveRemoteTranscodeConfig,
} from "@/lib/api/transcode-worker";

const BYTES_PER_MIB = 1024 * 1024;
/** Worker 在线状态的轮询间隔；配这个页面时用户就盯着它看，要够跟手。 */
const STATUS_POLL_MS = 5000;
/**
 * 单个 HLS 产物的上传上限，固定 512 MiB，不再让用户填。
 *
 * 它从来不是偏好，是 Worker 内存上传代理的实现上限——服务端的
 * DEFAULT 与 MAX 是同一个数（settings/remote_transcode.py），所以这个值
 * **只能往下调，而往下调只有坏处**：实际分片是 4 秒的 fMP4，通常几 MB 到
 * 几十 MB，离 512 MiB 差两个数量级；调到分片大小以下，播放会在上传阶段
 * 撞 413 失败。NAS 侧是流式落盘、不占内存，调低也省不出任何东西。
 *
 * 一个只能把事情弄坏、用户又无从判断该填多少的输入框，不该出现在界面上。
 */
const ARTIFACT_LIMIT_BYTES = 512 * BYTES_PER_MIB;
const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-sub " +
  "text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
  "focus:border-[var(--accent)]/50";

export interface RemoteTranscodeSectionProps {
  /** 去「设备」分区审批或吊销 Worker。批准是这条链路的必经一步，得能一键到。 */
  onOpenDevices?: () => void;
}

/**
 * 「应用 → 远程转码」设置。
 *
 * 这一页要人做的决定只剩一个：开还是不开。
 *
 * Worker 用哪个地址连过来，是在 Mac 那侧填的；服务端下发任务时用的取源地址和
 * 产物回传地址，默认直接取用那条控制连接自报的地址（remote_worker.py 的
 * observed_base_url）。所以地址在这一页降级成「高级」里的覆盖项，只服务于
 * 反向代理改写 Host 的少数部署，也不再和系统外部访问地址有任何关系。
 */
export function RemoteTranscodeSection({ onOpenDevices }: RemoteTranscodeSectionProps) {
  const [config, setConfig] = useState<RemoteTranscodeConfigView | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [baseURLDraft, setBaseURLDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [status, setStatus] = useState<RemoteTranscodeStatus | null>(null);
  // 等待批准的 Worker 接入请求。用户在 Mac 上点完「请求接入」通常会切回浏览器，
  // 而他多半落在这一页（他是来配远程转码的）——不在这里提示，他看到的就是
  // 「还没有 Worker 连上来」，完全不知道有个请求正等他批。
  const [pendingWorkers, setPendingWorkers] = useState<DeviceRequestView[]>([]);
  // 已授权的 Worker。运行时注册表在断线时会把 Worker 整个摘掉
  // （remote_worker.py 的 unregister），所以只看 status.workers 的话，Mac
  // 一关机这台设备就从页面上凭空消失，用户会以为自己从来没配过、跟着引导
  // 又配一遍。授权是持久的，这份清单补上「配过但现在没连着」的那些。
  const [authorizedWorkers, setAuthorizedWorkers] = useState<DeviceTokenView[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getRemoteTranscodeConfig();
      setConfig(next);
      setEnabled(next.enabled);
      setBaseURLDraft(next.base_url_override);
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
      try {
        const [requests, devices] = await Promise.all([listDeviceRequests(), listDevices()]);
        if (!alive) return;
        setPendingWorkers(requests.filter((r) => r.client_type === "worker"));
        setAuthorizedWorkers(devices.filter((d) => d.client_type === "worker"));
      } catch {
        if (alive) {
          setPendingWorkers([]);
          setAuthorizedWorkers([]);
        }
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
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const next = await saveRemoteTranscodeConfig({
        enabled,
        base_url: baseURLDraft.trim(),
        // 存量若被调低过，这一步顺手拉回默认值——高上限只会更少地误伤
        max_artifact_bytes: ARTIFACT_LIMIT_BYTES,
      });
      setConfig(next);
      setEnabled(next.enabled);
      setBaseURLDraft(next.base_url_override);
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
  // 已授权但此刻没连上的：按名字和运行时列表对齐。名字两边都取 Mac 设置里的
  // 「Worker 名称」（配对时提交的 client_name 就是它），正常情况对得上；
  // 用户配对后又改了名字才会多出一条，那种情况显示两行也不算错——确实有一份
  // 旧授权还挂着，去设备页吊销即可。
  const liveNames = new Set((status?.workers ?? []).map((w) => w.worker_id));
  const offlineWorkers = authorizedWorkers.filter((d) => !liveNames.has(d.name));
  const hasAnyWorker = (status?.workers.length ?? 0) > 0 || offlineWorkers.length > 0;
  // 开关打开 ≠ Worker 连上了。这两件事分开说，用户才知道下一步该干什么：
  // 前者不满足要打开开关（或改正「高级」里填错的覆盖地址），后者不满足
  // 要去 Mac 上看 App。
  const statusText = !config.enabled
    ? "已关闭"
    : !config.ready
      ? "「高级」里的覆盖地址不合法，暂不会分配远程转码任务"
      : onlineWorkers.length > 0
        ? `已就绪，${onlineWorkers.length} 个 Worker 在线`
        : "配置已就绪，但还没有 Worker 连上来";
  const statusClass = !config.enabled
    ? "text-[var(--text-muted)]"
    : config.ready && onlineWorkers.length > 0
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
        <h3 className="group-label mb-2.5 px-1">状态</h3>
        <div className="css-glass space-y-4 !rounded-2xl p-5 max-sm:p-4">
          {/* 开关刻意不依赖「有没有 Worker 连着」，两个方向的理由都很硬：
              · 关着时 Worker 连 WebSocket 都握不上（routes/transcode_worker.py
                的 remote_worker_enabled 闸），要求「先有连接才能开」是死锁；
              · Worker 会掉线（Mac 睡眠、关机、网络抖动）。开关跟着连接走，
                意味着睡一觉起来设置被改了，或者想关都关不掉。
              开关是**意图**，连接是**现实**，绑在一起就等于掉线即失忆。
              开着而没有 Worker 也无害：决策层每次播放都查 remote_worker_available，
              没有就走本地。所以这里不禁用，只把两个方向的后果说清楚。 */}
          <label className="flex cursor-pointer items-center justify-between gap-4">
            <span>
              <span className="block text-body font-medium text-[var(--text)]">启用远程硬件转码</span>
              <span className="mt-0.5 block text-caption leading-5 text-[var(--text-faint)]">
                {enabled
                  ? "没有 Worker 在线时，播放自动回落到 NAS 本地转码，不会失败"
                  : "关闭后已配对的 Worker 也会断开连接，所有播放走 NAS 本地转码"}
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

      {/* Worker 单独成组，不再埋在状态卡的底部：配完之后「成没成」全靠这一块
          回答，它是这一页最该被看见的内容。 */}
      <section>
        <h3 className="group-label mb-2.5 px-1">Worker</h3>
        <div className="css-glass space-y-4 !rounded-2xl p-5 max-sm:p-4">
          {/* 有请求在等批准时置顶。这是新授权流程下最容易卡住的一步：
              Mac 那边已经点了「请求接入」，人却在这一页找不到任何线索。 */}
          {pendingWorkers.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-300/25 bg-amber-300/[0.07] px-4 py-3">
              <p className="text-sub leading-relaxed text-amber-100">
                有 {pendingWorkers.length} 台 Worker 正在等待批准
                <span className="ml-1.5 font-mono text-caption text-amber-200/80">
                  {pendingWorkers.map((r) => r.user_code).join(" · ")}
                </span>
              </p>
              {onOpenDevices && (
                <button
                  type="button"
                  onClick={onOpenDevices}
                  className="btn-glass shrink-0 rounded-full px-3.5 py-1.5 text-sub font-medium"
                >
                  去审批
                </button>
              )}
            </div>
          )}

          {/* Worker 的 WebSocket 被 remote_worker_enabled（开关打开 AND 地址
              合法）挡着。做成横幅而不是替换整块内容：已授权的设备该照常列出来，
              用户需要同时看到「我配过哪几台」和「现在为什么连不上」。 */}
          {!config.ready && (
            <div className="rounded-xl border border-amber-300/25 bg-amber-300/[0.07] px-4 py-3 text-sub leading-relaxed text-amber-100">
              {!config.enabled
                ? "远程转码还没开启，Worker 现在连不上来。打开上面的开关并保存即可。"
                : "「高级」里填的覆盖地址不合法，Worker 现在连不上来。改正或清空它即可。"}
            </div>
          )}

          {status == null ? (
            <p className="text-caption text-[var(--text-faint)]">正在获取 Worker 状态…</p>
          ) : hasAnyWorker ? (
            <>
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
                {/* 已授权但没连上来的。它们在运行时注册表里不存在，但授权还在，
                    用户也确实配过——不显示的话，Mac 一关机这台就凭空消失，
                    引导还会催他重新配对一遍。 */}
                {offlineWorkers.map((device) => (
                  <li
                    key={device.id}
                    className="rounded-xl border border-white/[0.06] bg-black/15 px-3 py-2"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sub font-medium text-[var(--text-muted)]">
                        {device.name}
                      </span>
                      <span className="text-caption text-[var(--text-faint)]">未连接</span>
                    </div>
                    <p className="mt-1 text-caption leading-5 text-[var(--text-faint)]">
                      已授权 · 最近活跃 {relativeTime(device.last_used_at)}
                      {config.ready ? " · Mac 没开机或没联网时属正常" : ""}
                    </p>
                  </li>
                ))}
              </ul>
              {/* 这一页只回答「现在连着吗、在干什么」；「授权还在不在、要不要
                  吊销」是设备页的事，两页各管一段，互相指路。 */}
              {onOpenDevices && (
                <button
                  type="button"
                  onClick={onOpenDevices}
                  className="text-caption text-[var(--accent)] underline decoration-dotted underline-offset-2"
                >
                  在「设备」里查看授权或吊销
                </button>
              )}
            </>
          ) : config.ready ? (
            <div className="space-y-2 text-caption leading-5 text-[var(--text-faint)]">
              <p className="text-sub text-[var(--text-muted)]">还没有 Worker 接入。在 Mac 上：</p>
              <ol className="space-y-1 pl-4">
                <li>1. 打开 MovieClaw Transcoder，点「在局域网中查找」或直接填 movieclaw 地址；</li>
                <li>2. 点「连接并配对」，它会显示一段配对码；</li>
                <li>
                  3. 回到网页的「设置 → 设备」，核对配对码后批准
                  {onOpenDevices && (
                    <>
                      {" "}
                      <button
                        type="button"
                        onClick={onOpenDevices}
                        className="text-[var(--accent)] underline decoration-dotted underline-offset-2"
                      >
                        去设备页
                      </button>
                    </>
                  )}
                  。
                </li>
              </ol>
              <p className="pt-1">
                全程不需要在任何一边输入令牌——Worker 的凭证是批准时签发的，直接回到那台
                Mac，不经过屏幕。
              </p>
            </div>
          ) : null}
        </div>
      </section>

      {/* 地址不再是必填项，所以这一组默认收起来。
          服务端下发任务时会用「这台 Worker 自己连上来的地址」拼源视频 URL 和
          产物上传 URL（remote_worker.py 的 observed_base_url）——Worker 刚从
          那个地址握上手，它必然够得着，没有任何理由再让人抄一遍。展开项只为
          反向代理改写了 Host、推断失真的少数部署保留。 */}
      <section>
        <h3 className="group-label mb-2.5 px-1">高级</h3>
        <div className="css-glass !rounded-2xl p-5 max-sm:p-4">
          <details open={Boolean(config.base_url)} className="group">
            <summary className="cursor-pointer list-none text-body font-medium text-[var(--text)] marker:hidden">
              取源与回传地址
              <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                {config.base_url_source === "worker_connection"
                  ? "自动"
                  : `已覆盖为 ${config.base_url}`}
              </span>
            </summary>
            <div className="mt-4 space-y-3">
              <p className="text-caption leading-5 text-[var(--text-faint)]">
                服务端下发任务时要告诉 Worker「去哪儿取源视频、往哪儿传 HLS 产物」。
                <b className="font-medium text-[var(--text-muted)]">
                  默认自动取用这台 Worker 连上来时用的地址
                </b>
                ，不需要设置——它刚从那儿握上手，必然够得着，而且通常就是最快的那条
                内网路径。只有当反向代理把 Host 改写成了上游地址（如 127.0.0.1:8000），
                导致 Worker 拿到的地址回不来时，才需要在这里指定一个 Worker 够得着的地址。
              </p>
              <div>
                <label
                  htmlFor="remote-base-url"
                  className="text-sub font-medium text-[var(--text-muted)]"
                >
                  覆盖地址
                </label>
                <input
                  id="remote-base-url"
                  type="url"
                  value={baseURLDraft}
                  onChange={(event) => setBaseURLDraft(event.target.value)}
                  placeholder="留空 = 自动（推荐）"
                  className={`mt-2 ${INPUT_CLASS}`}
                  spellCheck={false}
                />
              </div>
              <p className="text-caption leading-5 text-[var(--text-faint)]">
                {config.base_url_source === "remote_transcode_setting"
                  ? "当前使用上面填写的覆盖地址。"
                  : "当前为自动：每台 Worker 各用自己连上来的地址。"}
              </p>
            </div>
          </details>
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
