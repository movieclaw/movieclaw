"use client";

import { useState } from "react";

import { InfoIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import { restartApp } from "@/lib/api/app";
import { getHealth } from "@/lib/api/health";

/**
 * 应用维护（设置 → 更新与维护，「维护」标签）。
 *
 * 设置页按功能重组后，原「应用」分区的网络字段（外部访问地址 / 对外端口）
 * 迁去了「网络」分区（external-access-section.tsx），远程转码迁去了「播放」
 * 分区——这里只剩重启入口。
 *
 * 重启流程：调用 /app/restart → 后端优雅停机、以约定码 42 退出 → Docker 镜像
 * 的 entrypoint 重启循环只重启后端（前端保持运行，反代链路验证健康后恢复监督，
 * 不依赖 restart 策略；源码部署需 systemd 等守护）→ 前端轮询 /health 直到服务
 * 恢复，然后整页刷新。后端重启失败时 entrypoint 会升级为前后端完整重启，
 * 因此轮询窗口仍需覆盖完整重启的最坏情况。
 */

type RestartPhase = "idle" | "confirming" | "waiting" | "timeout";

export function AppMaintenanceSection() {
  const [restartPhase, setRestartPhase] = useState<RestartPhase>("idle");

  /** 重启：请求后端优雅停机，然后轮询 /health 等服务恢复，恢复后整页刷新。 */
  const doRestart = async () => {
    setRestartPhase("waiting");
    try {
      await restartApp();
    } catch {
      // 请求可能因进程退出而中断，属预期，继续轮询等恢复
    }
    // 先给停机留出时间，避免轮询打到「还没退出的旧进程」造成误判
    await new Promise((r) => setTimeout(r, 4000));
    // 轮询窗口需覆盖 entrypoint 重启链路的最坏情况（收尾宽限 + 后端就绪 +
    // 前端就绪各阶段的超时之和约 140s），否则会误报「超时」而服务随后自行恢复
    for (let i = 0; i < 75; i++) {
      try {
        await getHealth();
        window.location.reload();
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    setRestartPhase("timeout");
  };

  // 重启等待态：全区替换为状态页，避免用户在服务不可用期间继续操作
  if (restartPhase === "waiting" || restartPhase === "timeout") {
    return (
      <div className="css-glass !rounded-2xl px-6 py-10 text-center">
        {restartPhase === "waiting" ? (
          <>
            <p className="text-body font-medium text-[var(--text)]">正在重启应用…</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              服务恢复后页面会自动刷新，通常需要几秒到几十秒。
            </p>
          </>
        ) : (
          <>
            <p className="text-body font-medium text-[var(--text)]">等待超时，应用尚未恢复</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              Docker 部署通常几秒内自动拉起，请稍后手动刷新页面；源码部署且无
              systemd 等守护时，需要到服务器上手动启动。
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

  return (
    <div className="space-y-5">
      <section>
        <h3 className="group-label mb-2.5 px-1">维护</h3>
        <div className="css-glass !rounded-2xl">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <span className="flex shrink-0 items-center gap-1.5">
              <span className="text-body font-medium text-[var(--text)]">重启应用</span>
              <Tooltip
                content={
                  <>
                    <p>优雅停机后重新启动后端服务，正在进行的任务会中断。</p>
                    <p className="mt-1.5">
                      Docker 部署由容器入口自动拉起新进程，通常几秒内恢复；源码部署需有
                      systemd 等守护，否则退出后要到服务器上手动启动。
                    </p>
                  </>
                }
                placement="top"
                maxWidth={340}
              >
                <button
                  type="button"
                  aria-label="说明"
                  className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
                >
                  <InfoIcon className="size-[15px]" />
                </button>
              </Tooltip>
            </span>
            <button
              type="button"
              onClick={() => setRestartPhase("confirming")}
              className="btn-glass shrink-0 px-3.5 py-1.5 text-sub font-semibold text-red-300/90 hover:text-red-200"
            >
              重启应用
            </button>
          </div>
        </div>
      </section>

      {/* 重启二次确认 */}
      {restartPhase === "confirming" && (
        <div className="flex items-center justify-between gap-4 rounded-xl border border-red-300/25 bg-red-400/10 px-4 py-3">
          <p className="text-sub text-red-200/90">
            确认重启应用？重启期间服务短暂不可用，正在进行的下载投递/整理任务会中断。
          </p>
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => void doRestart()}
              className="btn-accent rounded-full px-3.5 py-1.5 text-sub font-semibold"
            >
              确认重启
            </button>
            <button
              type="button"
              onClick={() => setRestartPhase("idle")}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              取消
            </button>
          </span>
        </div>
      )}
    </div>
  );
}
