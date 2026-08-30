"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ChevronRightIcon, RefreshIcon } from "@/components/icons";
import { Markdown } from "@/components/markdown";
import { Modal } from "@/components/modal";
import {
  type ModelUpdateCheckView,
  type RollbackOptionsView,
  type RollbackTargetView,
  type UpdateCheckView,
  type UpdateProgressView,
  type UpdateStatusView,
  applyModelUpdate,
  applyUpdate,
  checkModelUpdate,
  checkUpdate,
  getPendingUpdate,
  getRollbackOptions,
  getUpdateProgress,
  getUpdateStatus,
  rollbackTo,
  saveUpdateRetention,
} from "@/lib/api/app";
import { getHealth } from "@/lib/api/health";
import { formatBytes } from "@/lib/format";
import { formatDateTime, formatUnixDateTime } from "@/lib/time";

/**
 * 版本与更新（设置 → 更新与维护）。
 *
 * 应用内更新的用户界面（机制见 docs/design/in-app-update.md）：
 *   - 状态区：当前版本 + 代码来源（镜像内置 / 应用内更新 vX / 源码部署）；
 *     曾启动失败被自动回落的版本在此外显。
 *   - **进页即知**：挂载时读服务端的待更新快照（每小时的定时检查早就查过了），
 *     有新版就直接把版本卡片摆出来。用户是被侧栏的更新入口引导过来的，到了
 *     这一页还得自己找一颗「检查更新」按钮点一下才看得见新版本，链路是断的；
 *     按钮保留，语义降级为"我现在就要重查一次"。
 *   - 检查更新：比对 GitHub 最新 Release。依赖没变（runtime 匹配）就能
 *     一键更新；依赖变了明确提示需升级 Docker 镜像。
 *   - 更新执行：后端后台下载校验，前端 1s 轮询进度；进入 restarting 后
 *     改为轮询 /health 等服务恢复（前后端全量重启），恢复即整页刷新。
 *   - 回退：切回上一版本（可再次回退撤销）；无上一版本时回落镜像内置版本。
 */

type RestartWait = "idle" | "waiting" | "timeout";

/** 新版本卡片的数据：手动检查结果与服务端快照两个来源归一到同一形状 */
interface AvailableUpdate {
  version: string;
  compatible: boolean;
  changelog: string;
  /** 该版本曾在本机连续启动失败被回落（只有手动检查结果带这一位；
   *  快照侧这类版本压根不会被记为"可更新"，故恒为 false） */
  knownBad: boolean;
}

export function AppUpdateSection() {
  const [status, setStatus] = useState<UpdateStatusView | null>(null);
  const [failed, setFailed] = useState(false);
  const [check, setCheck] = useState<UpdateCheckView | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [progress, setProgress] = useState<UpdateProgressView | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [modelCheck, setModelCheck] = useState<ModelUpdateCheckView | null>(null);
  const [modelChecking, setModelChecking] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  // 服务端待更新快照（进页预填的数据源）。一旦用户手动检查过，改由 check /
  // modelCheck 接管——手动检查的结论更新，不能被这份旧快照盖回去
  const [pendingVersion, setPendingVersion] = useState<AvailableUpdate | null>(null);
  const [pendingModelTag, setPendingModelTag] = useState<string | null>(null);
  const [restartWait, setRestartWait] = useState<RestartWait>("idle");
  // 回退选择器：候选列表（含保留策略现状）在分区挂载时就拉一次——回退卡的
  // 描述与「本地保留版本数」设置行都要用它，不是只有打开弹窗才需要
  const [rollback, setRollback] = useState<RollbackOptionsView | null>(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [retentionBusy, setRetentionBusy] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // 组件已卸载标记：waitForRestart 的长循环不能在用户离开设置页后还整页刷新
  const unmounted = useRef(false);
  // 进度轮询的连续失败计数：单次瞬时错误（反代 502、网络抖动）绝不能被
  // 误判成「后端已进入重启」——那会提前刷新页面并永久丢失更新跟踪
  const pollFailStreak = useRef(0);

  const reload = useCallback(() => {
    setFailed(false);
    getUpdateStatus()
      .then(setStatus)
      .catch(() => setFailed(true));
  }, []);

  /** 全量重启的等待：先观察到服务不可达（sawDown）、再观察到恢复才算完成
   *  一次真实重启——否则可能命中「还没退出的旧进程」提前刷新，刷新后再无
   *  任何轮询跟踪，页面在真正重启时静默失联。 */
  const waitForRestart = useCallback(async () => {
    if (unmounted.current) return;
    setRestartWait("waiting");
    let sawDown = false;
    for (let i = 0; i < 90; i++) {
      if (unmounted.current) return;
      try {
        await getHealth();
        // 恢复成立的两种情况：见过一次不可达（真实重启完成）；或等了足够久
        // 都没见到不可达（重启在两次探测间隙内完成，30 次 ≈ 60 秒后放行）
        if (sawDown || i >= 30) {
          window.location.reload();
          return;
        }
      } catch {
        sawDown = true;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    if (!unmounted.current) setRestartWait("timeout");
  }, []);

  /** 更新执行中：轮询进度直到失败或进入重启阶段。 */
  const pollProgress = useCallback(() => {
    if (pollTimer.current) clearInterval(pollTimer.current);
    pollFailStreak.current = 0;
    pollTimer.current = setInterval(async () => {
      try {
        const p = await getUpdateProgress();
        pollFailStreak.current = 0;
        setProgress(p);
        if (p.phase === "failed") {
          if (pollTimer.current) clearInterval(pollTimer.current);
        } else if (p.phase === "restarting") {
          if (pollTimer.current) clearInterval(pollTimer.current);
          void waitForRestart();
        } else if (p.phase === "idle") {
          // 活跃阶段之后读到 idle = 后端已重启完毕（新进程进度是空的）：
          // 更新已生效，直接刷新页面载入新版
          if (pollTimer.current) clearInterval(pollTimer.current);
          window.location.reload();
        }
      } catch {
        // 连续 3 次失败才认定「后端进入重启」，单次瞬时错误继续轮询
        pollFailStreak.current += 1;
        if (pollFailStreak.current >= 3) {
          if (pollTimer.current) clearInterval(pollTimer.current);
          void waitForRestart();
        }
      }
    }, 1000);
  }, [waitForRestart]);

  useEffect(() => {
    unmounted.current = false;
    reload();
    // 恢复进行中的更新跟踪：页面刷新/中途进入设置页时，后台线程可能正在
    // 下载安装（进度在后端单例里）。不恢复的话 UI 会显示空闲、按钮可点，
    // 全量重启来临时页面毫无预警地失联。
    getUpdateProgress()
      .then((p) => {
        if (["checking", "downloading", "verifying", "applying"].includes(p.phase)) {
          setProgress(p);
          pollProgress();
        } else if (p.phase === "restarting") {
          setProgress(p);
          void waitForRestart();
        } else if (p.phase === "failed" && p.error) {
          setProgress(p);
        }
      })
      .catch(() => undefined);
    // 进页预填：读定时检查留下的快照，有新版就直接摆出卡片（读库不触网）
    getPendingUpdate()
      .then((p) => {
        setPendingVersion(
          p.app_version
            ? {
                version: p.app_version,
                compatible: p.app_compatible,
                changelog: p.app_changelog,
                knownBad: false,
              }
            : null,
        );
        setPendingModelTag(p.model_tag);
      })
      .catch(() => undefined); // 快照读不到就退回"手动点检查更新"的老路径
    // 回退候选与保留策略（纯本地读盘）；失败不打扰——回退区自然不显示
    getRollbackOptions()
      .then(setRollback)
      .catch(() => undefined);
    return () => {
      unmounted.current = true;
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, [reload, pollProgress, waitForRestart]);

  const doCheck = async () => {
    setChecking(true);
    setCheckError(null);
    setCheck(null);
    try {
      setCheck(await checkUpdate());
    } catch (e) {
      setCheckError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  const doApply = async () => {
    setActionError(null);
    try {
      setProgress(await applyUpdate());
      pollProgress();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const doModelCheck = async () => {
    setModelChecking(true);
    setModelError(null);
    setModelCheck(null);
    try {
      setModelCheck(await checkModelUpdate());
    } catch (e) {
      setModelError((e as Error).message);
    } finally {
      setModelChecking(false);
    }
  };

  const doModelApply = async () => {
    setModelError(null);
    try {
      setProgress(await applyModelUpdate());
      pollProgress();
    } catch (e) {
      setModelError((e as Error).message);
    }
  };

  /** 执行回退：restore 由目标的数据兼容判定决定（restore 档自动带上恢复）。 */
  const doRollbackTo = async (target: RollbackTargetView) => {
    setRollbackOpen(false);
    setActionError(null);
    try {
      await rollbackTo(
        target.kind === "baseline" ? "baseline" : (target.version ?? ""),
        target.schema_action === "restore",
      );
      void waitForRestart();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  /** 调整本地保留版本数（立即生效并按新策略清理）。 */
  const doSetRetention = async (value: number) => {
    if (!rollback || retentionBusy) return;
    const next = Math.min(20, Math.max(2, value));
    if (next === rollback.keep_versions) return;
    setRetentionBusy(true);
    try {
      await saveUpdateRetention(next);
      setRollback(await getRollbackOptions()); // 清理后占用与候选都可能变化
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setRetentionBusy(false);
    }
  };

  if (failed) {
    return (
      <div className="flex items-center gap-3">
        <p className="text-ui text-[var(--text-muted)]">版本信息加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-3 py-1.5 text-sub font-medium">
          重试
        </button>
      </div>
    );
  }
  if (!status) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载版本信息…</p>;
  }

  // 重启等待态：全区替换为状态页（与应用设置的重启流程同款体验）
  if (restartWait !== "idle") {
    return (
      <div className="css-glass !rounded-2xl px-6 py-10 text-center">
        {restartWait === "waiting" ? (
          <>
            <p className="text-body font-medium text-[var(--text)]">正在重启并切换版本…</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              前后端会一起重启，服务恢复后页面自动刷新，通常需要几十秒。
            </p>
          </>
        ) : (
          <>
            <p className="text-body font-medium text-[var(--text)]">等待超时，应用尚未恢复</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              请稍后手动刷新页面。若反复无法恢复，容器会自动回落到更新前的版本，
              数据不受影响。
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="btn-glass mt-4 px-3.5 py-1.5 text-sub font-medium"
            >
              刷新页面
            </button>
          </>
        )}
      </div>
    );
  }

  const updating =
    progress != null && !["idle", "failed"].includes(progress.phase);
  // 新版本卡片的数据来源：手动检查过就以检查结果为准（哪怕结论是"已最新"，
  // 也要盖掉进页时那份可能已过时的快照），没检查过才用快照预填
  const available: AvailableUpdate | null = check
    ? check.update_available
      ? {
          version: check.latest_version,
          compatible: check.compatible,
          changelog: check.changelog,
          knownBad: check.latest_known_bad,
        }
      : null
    : pendingVersion;
  // 模型侧同理：手动检查结果优先，否则用快照
  const availableModelTag = modelCheck
    ? modelCheck.update_available && modelCheck.installable
      ? modelCheck.latest_tag
      : null
    : pendingModelTag;
  const sourceLabel =
    status.code_source === "overlay"
      ? `应用内更新版本${status.overlay_version ? ` v${status.overlay_version}` : ""}`
      : status.code_source === "baseline"
        ? "Docker 镜像内置"
        : "源码部署";

  return (
    <div className="space-y-7">
      {/* —— 版本（状态与动作同区：「检查更新」就挂在当前版本行的右侧，
             与 macOS 软件更新/Windows Update 的版式一致；空闲态不再为一颗
             按钮单开一个分组）—— */}
      <section>
        <h3 className="group-label mb-2.5 px-1">版本</h3>
        <div className="space-y-3">
          <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
            <div className="px-5 py-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-ui font-medium text-[var(--text)]">当前版本</p>
                  <p className="mt-0.5 text-sub text-[var(--text-muted)]">来源：{sourceLabel}</p>
                </div>
                <span className="flex flex-wrap items-center justify-end gap-3">
                  <span className="font-mono text-body text-[var(--text)]">
                    v{status.current_version}
                  </span>
                  {status.can_update && !updating && (
                    <button
                      type="button"
                      onClick={doCheck}
                      disabled={checking}
                      className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
                    >
                      <RefreshIcon className={`size-4 ${checking ? "animate-spin" : ""}`} />
                      <span>{checking ? "正在检查…" : "检查更新"}</span>
                    </button>
                  )}
                </span>
              </div>
              {/* 手动检查的即时反馈：跟在动作所在的行块下方，右对齐向按钮看齐 */}
              {check && !check.update_available && (
                <p className="mt-1.5 text-right text-caption text-emerald-300/90">
                  已是最新版本（v{check.current_version}）
                </p>
              )}
              {checkError && (
                <p className="mt-1.5 text-right text-caption text-red-300">{checkError}</p>
              )}
              {!status.can_update && (
                <p className="mt-1.5 text-right text-caption text-[var(--text-faint)]">
                  仅 Docker 镜像部署支持应用内更新；源码部署请用 git pull 更新
                </p>
              )}
            </div>
            {status.inactive_overlay_version && (
              <div className="px-5 py-3.5">
                <p className="text-sub text-[var(--text-muted)]">
                  已安装的 v{status.inactive_overlay_version} 未在运行
                  {status.inactive_overlay_reason ? `：${status.inactive_overlay_reason}` : ""}
                </p>
              </div>
            )}
            {status.bad_versions.length > 0 && (
              <div className="px-5 py-3.5">
                <p className="text-sub text-amber-300/90">
                  版本 {status.bad_versions.map((v) => `v${v}`).join("、")} 曾连续启动失败，
                  已被自动回落保护。可在新版本发布后重新更新。
                </p>
              </div>
            )}
            {status.last_abnormal_exit && (
              <div className="px-5 py-3.5">
                <p className="text-sub text-amber-300/90">
                  应用曾于 {formatUnixDateTime(status.last_abnormal_exit.at)}{" "}
                  异常退出并被容器自动恢复：{status.last_abnormal_exit.detail}
                  （exit={status.last_abnormal_exit.exit_code}）。若频繁出现，请查看容器日志排查。
                </p>
              </div>
            )}
          </div>

          {/* 更新进度：紧跟版本卡，替换新版本卡片的位置 */}
          {updating && progress && (
            <div className="css-glass !rounded-2xl px-5 py-4">
              <p className="text-ui font-medium text-[var(--text)]">
                {progress.detail || "正在更新…"}
              </p>
              {progress.phase === "downloading" && progress.percent != null && (
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
                  <div
                    className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500"
                    style={{ width: `${progress.percent}%` }}
                  />
                </div>
              )}
              <p className="mt-2 text-sub text-[var(--text-muted)]">
                更新在后台执行，完成后会自动重启并刷新页面。
              </p>
            </div>
          )}

          {/* 新版本卡片：进页由快照预填，手动检查后以检查结果为准 */}
          {!updating && available && (
            <div className="css-glass !rounded-2xl px-5 py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-ui font-medium text-[var(--text)]">
                  发现新版本 v{available.version}
                </p>
                {available.compatible ? (
                  <button
                    type="button"
                    onClick={doApply}
                    className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-[var(--accent)]"
                  >
                    立即更新
                  </button>
                ) : (
                  <span className="text-sub text-amber-300/90">
                    本次更新包含依赖变化，需拉取新的 Docker 镜像升级
                  </span>
                )}
              </div>
              {available.knownBad && (
                <p className="mt-2 text-sub text-amber-300/90">
                  注意：v{available.version} 此前曾在本机连续启动失败被自动回落。
                  重新更新会清除失败标记再试一次；若问题依旧，容器会再次自动回落，
                  建议等待修复版本。
                </p>
              )}
              {/* 更新说明是 GitHub Release 的 Markdown 原文，复用全站的
                  Markdown 渲染器（紧凑档） */}
              {available.changelog && (
                <div className="scroll-thin mt-3 max-h-64 overflow-y-auto pr-1">
                  <Markdown text={available.changelog} compact />
                </div>
              )}
            </div>
          )}
          {actionError && <p className="text-sub text-red-300">{actionError}</p>}
        </div>
      </section>

      {/* 上一次更新（应用或模型）异步失败的统一外显：失败可能发生在任一区
          发起的后台任务里，放在共享位置避免归属混乱 */}
      {progress?.phase === "failed" && progress.error && (
        <div className="css-glass !rounded-2xl px-5 py-3.5">
          <p className="text-sub text-red-300">
            上次更新{progress.target_version ? `（${progress.target_version}）` : ""}失败：
            {progress.error}
          </p>
        </div>
      )}

      {/* —— NER 模型 ——（独立于代码更新；生效需重新解析模型指针，走全量重启） */}
      {status.can_update && (
        <section>
          <h3 className="group-label mb-2.5 px-1">NER 识别模型</h3>
          <div className="css-glass !rounded-2xl px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-ui font-medium text-[var(--text)]">
                  当前模型：{status.model_tag ?? "无法识别（较早的镜像）"}
                </p>
                <p className="mt-0.5 text-sub text-[var(--text-muted)]">
                  种子名识别（NER）模型独立更新，无需升级镜像；更新后应用会自动重启并刷新页面。
                </p>
              </div>
              {!updating && (
                <button
                  type="button"
                  onClick={doModelCheck}
                  disabled={modelChecking}
                  className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
                >
                  {modelChecking ? "正在检查…" : "检查模型更新"}
                </button>
              )}
            </div>
            {/* 有可装的新模型：进页就摆出来（快照预填），手动检查后以检查结果为准 */}
            {availableModelTag && (
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <span className="text-sub text-[var(--text)]">发现新模型 {availableModelTag}</span>
                <button
                  type="button"
                  onClick={doModelApply}
                  disabled={updating}
                  className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-[var(--accent)] disabled:opacity-50"
                >
                  更新模型
                </button>
              </div>
            )}
            {/* 手动检查后的两种"装不了"的结论：只有点过按钮才说，避免进页就
                冒出一句无从触发的结论 */}
            {modelCheck && !availableModelTag && (
              <p className="mt-3 text-sub">
                {!modelCheck.update_available ? (
                  <span className="text-emerald-300/90">
                    模型已是最新（{modelCheck.latest_tag}）
                  </span>
                ) : (
                  <span className="text-amber-300/90">
                    发现新模型 {modelCheck.latest_tag}，但该发布未携带更新清单，暂无法应用内安装
                  </span>
                )}
              </p>
            )}
            {modelError && <p className="mt-2 text-sub text-red-300">{modelError}</p>}
          </div>
        </section>
      )}

      {/* —— 回退与版本保留 ——（有候选版本才出现回退入口；保留数设置常驻） */}
      {status.can_update && rollback && (
        <section>
          <h3 className="group-label mb-2.5 px-1">回退</h3>
          <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
            {rollback.targets.length > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <p className="text-sub text-[var(--text-muted)]">
                  更新后遇到问题时，可切换到本机保留的历史版本；跨数据库升级的回退
                  会恢复对应时点的自动备份。
                </p>
                <button
                  type="button"
                  onClick={() => setRollbackOpen(true)}
                  disabled={updating}
                  className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
                >
                  选择版本回退…
                </button>
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
              <div>
                <p className="text-ui font-medium text-[var(--text)]">本地保留版本数</p>
                <p className="mt-0.5 text-sub text-[var(--text-muted)]">
                  保留越多可回退的范围越大，占用磁盘越多
                  {rollback.versions_dir_bytes > 0
                    ? `（当前占用 ${formatBytes(rollback.versions_dir_bytes)}）`
                    : ""}
                </p>
              </div>
              <span className="flex items-center gap-2">
                <button
                  type="button"
                  aria-label="减少保留版本数"
                  onClick={() => doSetRetention(rollback.keep_versions - 1)}
                  disabled={retentionBusy || rollback.keep_versions <= 2}
                  className="btn-glass !size-8 justify-center !p-0 text-body disabled:opacity-40"
                >
                  −
                </button>
                <span className="tnum w-8 text-center text-body font-medium text-[var(--text)]">
                  {rollback.keep_versions}
                </span>
                <button
                  type="button"
                  aria-label="增加保留版本数"
                  onClick={() => doSetRetention(rollback.keep_versions + 1)}
                  disabled={retentionBusy || rollback.keep_versions >= 20}
                  className="btn-glass !size-8 justify-center !p-0 text-body disabled:opacity-40"
                >
                  +
                </button>
              </span>
            </div>
          </div>
        </section>
      )}

      <RollbackDialog
        open={rollbackOpen}
        onClose={() => setRollbackOpen(false)}
        targets={rollback?.targets ?? []}
        onConfirm={doRollbackTo}
      />
    </div>
  );
}

/**
 * 回退选择器弹窗：候选版本列表（新的在前）→ 选中展开详情（更新说明 + 数据
 * 兼容结论）→ 底部一颗写明落点与数据后果的确认按钮。
 *
 * 数据兼容三档的呈现（对应后端 schema_action）：
 *   switch  绿字「数据结构一致，直接切换，数据全保留」——零顾虑；
 *   restore 琥珀字明示「将恢复 X 时点的备份，此后数据丢失」——必须知情确认；
 *   unknown 红字强警告「无对应备份，不保证正常运行」——仍可执行但后果自负。
 */
function RollbackDialog({
  open,
  onClose,
  targets,
  onConfirm,
}: {
  open: boolean;
  onClose: () => void;
  targets: RollbackTargetView[];
  onConfirm: (target: RollbackTargetView) => void;
}) {
  // 选中项按索引记（targets 来自快照数据，弹窗生命周期内不变）
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    if (open) setSelected(null); // 每次打开都从未选状态开始，避免带着上次的选择
  }, [open]);

  const pick = selected != null ? targets[selected] : null;
  const label = (t: RollbackTargetView) =>
    t.kind === "baseline"
      ? `镜像内置版本${t.version ? ` v${t.version}` : ""}`
      : `v${t.version}`;

  return (
    <Modal
      open={open}
      onClose={onClose}
      label="选择回退版本"
      width="lg"
      panelClassName="flex max-h-[76dvh] flex-col"
    >
      <div className="px-5 pb-3 pt-5">
        <h2 className="text-title-sm font-semibold text-[var(--text)]">选择回退版本</h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">
          回退会重启应用；是否需要恢复数据备份取决于目标版本的数据结构差异，
          结论已在每一项里标明。
        </p>
      </div>
      <div className="scroll-thin flex-1 space-y-2 overflow-y-auto px-5 pb-4">
        {targets.map((t, i) => (
          // 展开区不放进 <button>：更新说明里可能有链接（Markdown），交互元素
          // 嵌套交互元素是非法结构。头部行是按钮，详情是它下方的兄弟块
          <div
            key={`${t.kind}-${t.version ?? "?"}`}
            className={`rounded-xl border transition-colors ${
              i === selected
                ? "border-white/[0.18] bg-white/[0.07]"
                : "border-white/[0.07] bg-white/[0.03] hover:bg-white/[0.05]"
            }`}
          >
            <button
              type="button"
              onClick={() => setSelected(i === selected ? null : i)}
              className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left"
            >
              <span className="text-ui font-semibold text-[var(--text)]">{label(t)}</span>
              {t.schema_action === "switch" ? (
                <span className="text-caption text-emerald-300/90">可直接切换，数据保留</span>
              ) : t.schema_action === "restore" ? (
                <span className="text-caption text-amber-300/90">
                  需恢复 {formatDateTime(t.backup_taken_at)} 的数据备份
                </span>
              ) : (
                <span className="text-caption text-red-300/90">无对应数据备份</span>
              )}
              <span className="ml-auto text-caption text-[var(--text-faint)]">
                {[
                  t.installed_at ? `装于 ${formatDateTime(t.installed_at)}` : null,
                  t.size_bytes != null ? formatBytes(t.size_bytes) : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              <ChevronRightIcon
                className={`size-4 shrink-0 text-[var(--text-faint)] transition-transform ${
                  i === selected ? "rotate-90" : ""
                }`}
              />
            </button>
            {/* 选中展开：数据后果 + 该版本的更新说明 */}
            {i === selected && (
              <div className="border-t border-white/[0.06] px-4 py-2.5">
                {t.schema_action === "switch" ? (
                  <p className="text-sub text-emerald-300/90">
                    该版本与当前的数据结构一致：直接切换代码，全部数据原样保留。
                  </p>
                ) : t.schema_action === "restore" ? (
                  <p className="text-sub text-amber-300/90">
                    当前数据结构比该版本新（中间经过数据库升级）。回退将恢复{" "}
                    {formatDateTime(t.backup_taken_at)} 自动备份的数据，
                    此后产生的数据（订阅活动、入库记录等）将丢失；回退前会再自动
                    备份一次当前数据，切回新版本时可完整还原。
                  </p>
                ) : (
                  <p className="text-sub text-red-300/90">
                    没有可对应时点的数据备份：切换后该版本将直接使用当前数据，
                    若中间经过数据库升级可能无法正常运行（启动失败时会自动回落）。
                  </p>
                )}
                {t.changelog && (
                  <div className="scroll-thin mt-2.5 max-h-48 overflow-y-auto pr-1">
                    <Markdown text={t.changelog} compact />
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      {/* 底栏：确认按钮写明落点与数据后果，这是点下去前看到的最后一句话 */}
      <div className="flex items-center justify-end gap-2 border-t border-white/[0.06] px-5 py-3.5">
        <button type="button" onClick={onClose} className="btn-glass px-3.5 py-1.5 text-sub font-medium">
          取消
        </button>
        <button
          type="button"
          disabled={pick == null}
          onClick={() => pick && onConfirm(pick)}
          className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-red-300 disabled:opacity-40"
        >
          {pick == null
            ? "选择一个版本"
            : pick.schema_action === "restore"
              ? `回退到 ${label(pick)} 并恢复备份`
              : `回退到 ${label(pick)} 并重启`}
        </button>
      </div>
    </Modal>
  );
}
