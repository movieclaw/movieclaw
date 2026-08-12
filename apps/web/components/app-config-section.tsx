"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CheckIcon, InfoIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import {
  type AppConfigView,
  getAppConfig,
  restartApp,
  saveAppConfig,
} from "@/lib/api/app";
import { getHealth } from "@/lib/api/health";

/**
 * 应用设置（设置 → 应用设置）。
 *
 * 当前阶段只有「网络」一组配置 + 「维护」里的重启入口：
 *   - 外部访问地址：从网络上能访问到本应用的完整地址，保存即生效（纯落库数据，
 *     供生成通知链接、Agent 拼页面链接等绝对 URL 的场景使用）。
 *
 * 为什么没有端口设置：用户视角的访问入口是前端（Docker 默认 3000，对外端口由
 * compose 的 ports 映射决定），后端 8000 只在容器内被反代——两者都不是应用内
 * 能有意义配置的。
 *
 * 交互模型与「网络与代理」分区一致：输入框失焦自动落库，无「保存」按钮。
 *
 * 重启流程：调用 /app/restart → 后端优雅停机、以约定码 42 退出 → Docker 镜像
 * 的 entrypoint 重启循环只重启后端（前端保持运行，反代链路验证健康后恢复监督，
 * 不依赖 restart 策略；源码部署需 systemd 等守护）→ 前端轮询 /health 直到服务
 * 恢复，然后整页刷新。后端重启失败时 entrypoint 会升级为前后端完整重启，
 * 因此轮询窗口仍需覆盖完整重启的最坏情况。
 */

type RestartPhase = "idle" | "confirming" | "waiting" | "timeout";

export function AppConfigSection() {
  const [view, setView] = useState<AppConfigView | null>(null);
  const [failed, setFailed] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [urlError, setUrlError] = useState<string | null>(null);
  // 输入框草稿是否为空：有输入时收起框内的「使用」按钮，避免误点覆盖手填内容
  const [draftEmpty, setDraftEmpty] = useState(true);
  const [restartPhase, setRestartPhase] = useState<RestartPhase>("idle");
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 外部访问地址的最佳示例就是用户此刻浏览器正在使用的地址（SSR 阶段无 window，
  // 但表单要等客户端拉到配置才渲染，这里只是兜底给个通用示例）
  const currentOrigin =
    typeof window === "undefined" ? "https://movie.example.com" : window.location.origin;

  const reload = useCallback(() => {
    setFailed(false);
    getAppConfig()
      .then(setView)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
    return () => {
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
  }, [reload]);

  /** 落库一份完整配置（失焦触发），错误信息中文回显在对应字段下方。 */
  const commit = useCallback(
    (patch: Partial<Pick<AppConfigView, "external_url">>) => {
      if (!view) return;
      setSaveState("saving");
      setSaveError(null);
      saveAppConfig({
        external_url: patch.external_url ?? view.external_url,
      })
        .then((v) => {
          setView(v);
          setSaveState("saved");
          if (savedTimer.current) clearTimeout(savedTimer.current);
          savedTimer.current = setTimeout(() => setSaveState("idle"), 2000);
        })
        .catch((e) => {
          setSaveState("error");
          setSaveError((e as Error).message);
        });
    },
    [view],
  );

  const handleUrlBlur = (raw: string) => {
    const url = raw.trim();
    if (url && !/^https?:\/\/.+/.test(url)) {
      setUrlError("需以 http:// 或 https:// 开头的完整地址");
      return;
    }
    setUrlError(null);
    commit({ external_url: url });
  };

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

  if (failed) {
    return (
      <div className="flex items-center gap-3">
        <p className="text-ui text-[var(--text-muted)]">应用设置加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-3 py-1.5 text-sub font-medium">
          重试
        </button>
      </div>
    );
  }
  if (!view) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载应用设置…</p>;
  }

  // 重启等待态：全区替换为状态页，避免用户在服务不可用期间继续操作表单
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
    <div className="space-y-7">
      {/* —— 网络 —— */}
      <section>
        <div className="mb-2.5 flex h-5 items-center justify-between px-1">
          <h3 className="group-label">网络</h3>
          <span className="text-sub">
            {saveState === "saving" && <span className="text-[var(--text-faint)]">保存中…</span>}
            {saveState === "saved" && (
              <span className="flex items-center gap-1 text-emerald-300/90">
                <CheckIcon className="size-3.5" />
                已保存
              </span>
            )}
            {saveState === "error" && <span className="text-red-300">保存失败：{saveError}</span>}
          </span>
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {/* 外部访问地址 */}
          <div className="px-5 py-4">
            <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-stretch max-md:gap-2">
              <LabelWithHelp
                label="外部访问地址"
                help={
                  <>
                    <p>
                      从网络上能访问到本应用的完整地址，保存即生效。通常就是你浏览器
                      地址栏正在使用的地址（输入框的提示即当前地址，照填即可）。
                    </p>
                    <p className="mt-1.5">
                      若经反向代理 / 域名访问，请填代理后的对外地址，如{" "}
                      <code>https://movie.example.com</code>。
                    </p>
                    <p className="mt-1.5 text-[var(--text-muted)]">
                      用于生成通知里的跳转链接、对外回调地址等需要绝对 URL 的场景。
                    </p>
                  </>
                }
              />
              <div className="relative w-[300px] max-w-[55%] max-md:w-full max-md:max-w-none">
                <input
                  // key 随已保存值变化：一键填入/规范化（去尾斜杠）保存后，
                  // 非受控输入框靠重挂载同步显示最新落库值
                  key={view.external_url}
                  type="text"
                  defaultValue={view.external_url}
                  onChange={(e) => setDraftEmpty(e.target.value.trim() === "")}
                  onBlur={(e) => handleUrlBlur(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                  placeholder={currentOrigin}
                  className={`w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-sub text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50 ${
                    !view.external_url && draftEmpty ? "pr-14" : ""
                  }`}
                />
                {/* 框内快捷按钮：紧贴占位符（= 当前浏览器地址）末尾，点一下
                    直接落库；保存成功后 external_url 非空，按钮随之消失 */}
                {!view.external_url && draftEmpty && (
                  <button
                    type="button"
                    onClick={() => commit({ external_url: currentOrigin })}
                    disabled={saveState === "saving"}
                    title={`保存为 ${currentOrigin}`}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-lg bg-white/[0.1] px-2 py-1 text-caption font-semibold text-[var(--text-muted)] transition-colors hover:bg-[var(--accent-soft)] hover:text-[var(--accent)] disabled:opacity-40"
                  >
                    使用
                  </button>
                )}
              </div>
            </div>
            {urlError && <p className="mt-1.5 text-right text-caption text-red-300">{urlError}</p>}
            {/* 未设置时的引导小字：一句话说清动作与收益，完整说明在 ⓘ 里 */}
            {!view.external_url && !urlError && (
              <p className="mt-1.5 text-right text-caption leading-5 text-[var(--text-faint)]">
                点「使用」采用当前地址，通知与 AI 回复才能带上页面链接
              </p>
            )}
          </div>
        </div>
      </section>

      {/* —— 维护 —— */}
      <section>
        <h3 className="group-label mb-2.5 px-1">维护</h3>
        <div className="css-glass !rounded-2xl">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <LabelWithHelp
              label="重启应用"
              help={
                <>
                  <p>优雅停机后重新启动后端服务，正在进行的任务会中断。</p>
                  <p className="mt-1.5">
                    Docker 部署由容器入口自动拉起新进程，通常几秒内恢复；源码部署需有
                    systemd 等守护，否则退出后要到服务器上手动启动。
                  </p>
                </>
              }
            />
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

/** 字段名 + ⓘ 帮助（与「网络与代理」分区同款：说明收进 tooltip，页面只留字段）。 */
function LabelWithHelp({ label, help }: { label: string; help: React.ReactNode }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <span className="text-body font-medium text-[var(--text)]">{label}</span>
      <Tooltip content={help} placement="top" maxWidth={340}>
        <button
          type="button"
          aria-label="说明"
          className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
        >
          <InfoIcon className="size-[15px]" />
        </button>
      </Tooltip>
    </span>
  );
}
