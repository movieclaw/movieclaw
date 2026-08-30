"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LiquidGlassButton } from "@/vendor/liquid-glass";

import { ExternalAccessSection } from "@/components/external-access-section";
import { CheckIcon, ChevronRightIcon, InfoIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import { useBackdrop } from "@/lib/backdrop";
import {
  type NetworkConfigPayload,
  type NetworkConfigView,
  type NetworkTestResult,
  type ProxyMode,
  getNetworkConfig,
  saveNetworkConfig,
  testNetworkService,
} from "@/lib/api/network";

/**
 * 网络设置（设置 → 网络）。
 *
 * 交互模型：**自动保存，立即生效**。开关/模式点击即落库；地址输入框失焦落库；
 * 没有「保存」按钮，也就不存在「先保存才能测试」的中间态——「测试」随时可点，
 * 点击前会先冲刷未落库的修改。说明性文字一律收进 ⓘ tooltip，页面只留字段本身。
 *
 * 分组（macOS 设置式字段组）：
 *   代理 —— 方式三选一 + （手动）地址 /（环境变量）探测结果
 *   走代理的服务 —— 内置服务开关 + 每行连通性测试；PT 站点单独一张卡
 *   外部访问 —— 外部访问地址 / 对外端口（原「应用」分区迁来，
 *     见 external-access-section.tsx）
 *   镜像 / 反代地址 —— 高级项，默认折叠，沉底
 */

type TestState = { state: "pending" } | { state: "done"; result: NetworkTestResult };

const PROXY_URL_PATTERN = /^(http|https|socks5|socks5h):\/\//i;

export function NetworkConfigSection() {
  // 液态玻璃开关需要背景图采样（与搜索/站点/下载器设置同款）
  const { backdrop } = useBackdrop();
  const [view, setView] = useState<NetworkConfigView | null>(null);
  const [failed, setFailed] = useState(false);
  const [form, setForm] = useState<NetworkConfigPayload | null>(null);
  // 保存状态：idle 不占视觉；saving/saved/error 在页面右上角以一行小字反馈
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [proxyUrlError, setProxyUrlError] = useState<string | null>(null);
  const [mirrorErrors, setMirrorErrors] = useState<Record<string, string>>({});
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // 保存请求串行化：快速连点开关时按顺序落库，避免旧请求覆盖新配置
  const saveChain = useRef<Promise<unknown>>(Promise.resolve());
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reload = useCallback(() => {
    setFailed(false);
    getNetworkConfig()
      .then((v) => {
        setView(v);
        setForm({
          proxy_mode: v.proxy_mode,
          proxy_url: v.proxy_url,
          proxy_services: v.proxy_services,
          tmdb_api_base_url: v.tmdb_api_base_url,
          tmdb_image_base_url: v.tmdb_image_base_url,
          douban_api_base_url: v.douban_api_base_url,
        });
      })
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
    return () => {
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
  }, [reload]);

  /** 落库一份完整配置（串行）。手动模式地址为空/非法时只改表单不落库。 */
  const commit = useCallback((next: NetworkConfigPayload) => {
    setForm(next);
    if (next.proxy_mode === "manual") {
      const url = next.proxy_url.trim();
      if (!url) {
        // 刚切到手动、地址还没填：等地址失焦后再落库
        setProxyUrlError(null);
        return;
      }
      if (!PROXY_URL_PATTERN.test(url)) {
        setProxyUrlError("地址需以 http:// 、socks5:// 或 socks5h:// 开头");
        return;
      }
    }
    setProxyUrlError(null);
    setSaveState("saving");
    setSaveError(null);
    saveChain.current = saveChain.current.then(() =>
      saveNetworkConfig(next)
        .then((v) => {
          setView(v);
          // 出口配置变了，旧的测试结论不再可信
          setTests({});
          setSaveState("saved");
          if (savedTimer.current) clearTimeout(savedTimer.current);
          savedTimer.current = setTimeout(() => setSaveState("idle"), 2000);
        })
        .catch((e) => {
          setSaveState("error");
          setSaveError((e as Error).message);
        }),
    );
  }, []);

  const runTest = useCallback(
    (service: string) => {
      setTests((prev) => ({ ...prev, [service]: { state: "pending" } }));
      // 先冲刷在途的保存，测试才反映用户此刻看到的配置
      void saveChain.current.then(() =>
        testNetworkService(service)
          .then((result) => setTests((prev) => ({ ...prev, [service]: { state: "done", result } })))
          .catch((e) =>
            setTests((prev) => ({
              ...prev,
              [service]: {
                state: "done",
                result: { ok: false, latency_ms: null, message: (e as Error).message },
              },
            })),
          ),
      );
    },
    [],
  );

  const [builtinServices, siteServices] = useMemo(() => {
    const all = view?.services ?? [];
    return [all.filter((s) => !s.id.startsWith("site:")), all.filter((s) => s.id.startsWith("site:"))];
  }, [view]);

  if (failed) {
    return (
      <div className="flex items-center gap-3">
        <p className="text-ui text-[var(--text-muted)]">网络配置加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-3 py-1.5 text-sub font-medium">
          重试
        </button>
      </div>
    );
  }
  if (!view || !form) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载网络配置…</p>;
  }

  const proxyActive =
    form.proxy_mode === "manual"
      ? PROXY_URL_PATTERN.test(form.proxy_url.trim())
      : form.proxy_mode === "env" && Boolean(view.env_proxy_detected);

  return (
    <div className="space-y-7">
      {/* —— 代理 —— */}
      <section>
        {/* 保存状态挂在首个分组标题行右侧：不单占一行，避免页面顶部空隙 */}
        <div className="mb-2.5 flex h-5 items-center justify-between px-1">
          <h3 className="group-label">代理</h3>
          <span className="text-sub">
            {saveState === "saving" && <span className="text-[var(--text-faint)]">保存中…</span>}
            {saveState === "saved" && (
              <span className="flex items-center gap-1 text-emerald-300/90">
                <CheckIcon className="size-3.5" />
                已保存，立即生效
              </span>
            )}
            {saveState === "error" && <span className="text-red-300">保存失败：{saveError}</span>}
          </span>
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <LabelWithHelp
              label="代理方式"
              help={
                <>
                  <p><strong>不使用</strong>：全部服务直连。</p>
                  <p className="mt-1.5">
                    <strong>环境变量</strong>：代理地址取自 HTTPS_PROXY / ALL_PROXY 等环境变量，
                    Docker 部署用 <code>-e HTTPS_PROXY=…</code> 传入即可。
                  </p>
                  <p className="mt-1.5"><strong>手动</strong>：直接填写代理地址，支持 http 与 socks5。</p>
                  <p className="mt-1.5 text-[var(--text-muted)]">改动立即生效，无需重启。</p>
                </>
              }
            />
            <div className="flex shrink-0 rounded-full border border-white/10 bg-black/25 p-1">
              {(
                [
                  ["off", "不使用"],
                  ["env", "环境变量"],
                  ["manual", "手动"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => commit({ ...form, proxy_mode: value as ProxyMode })}
                  className={`rounded-full px-3.5 py-1.5 text-sub font-semibold transition ${
                    form.proxy_mode === value
                      ? "bg-white/15 text-white shadow-sm"
                      : "text-[var(--text-muted)] hover:text-white"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {form.proxy_mode === "env" && (
            <div className="flex items-center justify-between gap-4 px-5 py-3.5">
              <span className="text-sub text-[var(--text-muted)]">环境变量探测</span>
              {view.env_proxy_detected ? (
                <span className="font-mono text-sub text-[var(--text)]">{view.env_proxy_detected}</span>
              ) : (
                <span className="flex items-center gap-1.5 text-sub text-amber-300/90">
                  未发现代理地址
                  <HelpDot
                    content="未检测到 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY。Docker 部署可通过 -e HTTPS_PROXY=… 传入；或改用「手动」直接填写。"
                  />
                </span>
              )}
            </div>
          )}

          {form.proxy_mode === "manual" && (
            <div className="px-5 py-4">
              <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-stretch max-md:gap-2">
                <LabelWithHelp
                  label="代理地址"
                  help={
                    <>
                      <p>NAS 上跑 Clash / sing-box 等工具时，填它的 HTTP 或 SOCKS5 入站地址。</p>
                      <p className="mt-1.5">
                        例：<code>http://192.168.1.2:7890</code> 或 <code>socks5://192.168.1.2:7891</code>。
                        需要由代理端解析域名（对抗 DNS 污染）用 <code>socks5h://</code> 前缀。
                      </p>
                    </>
                  }
                />
                <input
                  type="text"
                  defaultValue={form.proxy_url}
                  onBlur={(e) => commit({ ...form, proxy_url: e.target.value.trim() })}
                  onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                  placeholder="socks5://192.168.1.2:7891"
                  className="w-[300px] max-w-[55%] rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-sub text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50 max-md:w-full max-md:max-w-none"
                />
              </div>
              {(proxyUrlError || !form.proxy_url.trim()) && (
                <p className={`mt-1.5 text-right text-caption ${proxyUrlError ? "text-red-300" : "text-[var(--text-faint)]"}`}>
                  {proxyUrlError ?? "填写地址后自动保存生效"}
                </p>
              )}
            </div>
          )}
        </div>
      </section>

      {/* —— 走代理的服务 —— */}
      <section>
        <div className="mb-2.5 flex items-center gap-1.5 px-1">
          <h3 className="group-label">走代理的服务</h3>
          <HelpDot
            content={
              <>
                <p>按服务选择流量是否经过上面的代理；内网的下载器、媒体服务器永远直连，不在此列。</p>
                <p className="mt-1.5">
                  经验默认：TMDB 与图片回源走代理（国内被墙）；豆瓣与 PT 站直连通常更快，
                  且部分 PT 站风控在意出口 IP，按需开启。
                </p>
                <p className="mt-1.5">「测试」按当前配置发一次真实请求，熔断中的服务测通后立即恢复。</p>
              </>
            }
          />
          {!proxyActive && (
            <span className="ml-auto text-caption text-[var(--text-faint)]">
              当前无可用代理，开关已禁用（测试仍可用，测的是直连/镜像的连通性）
            </span>
          )}
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {builtinServices.map((service) => (
            <ServiceRow
              key={service.id}
              label={service.label}
              description={service.description}
              backdrop={backdrop}
              enabled={form.proxy_services.includes(service.id)}
              toggleDisabled={!proxyActive}
              test={tests[service.id]}
              onTest={() => runTest(service.id)}
              onToggle={() =>
                commit({
                  ...form,
                  proxy_services: form.proxy_services.includes(service.id)
                    ? form.proxy_services.filter((s) => s !== service.id)
                    : [...form.proxy_services, service.id],
                })
              }
            />
          ))}
        </div>
      </section>

      {/* —— PT 站点（有已配置站点才出现）—— */}
      {siteServices.length > 0 && (
        <section>
          <div className="mb-2.5 flex items-center gap-1.5 px-1">
            <h3 className="group-label">PT 站点</h3>
            <HelpDot content="每个已配置的站点独立控制。国内 PT 站直连通常更快，且部分站点风控在意出口 IP——只给确实需要翻墙的站点开代理。" />
          </div>
          <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
            {siteServices.map((service) => (
              <ServiceRow
                key={service.id}
                label={service.label}
                description={service.description}
                backdrop={backdrop}
                enabled={form.proxy_services.includes(service.id)}
                toggleDisabled={!proxyActive}
                test={tests[service.id]}
                onTest={() => runTest(service.id)}
                onToggle={() =>
                  commit({
                    ...form,
                    proxy_services: form.proxy_services.includes(service.id)
                      ? form.proxy_services.filter((s) => s !== service.id)
                      : [...form.proxy_services, service.id],
                  })
                }
              />
            ))}
          </div>
        </section>
      )}

      {/* —— 外部访问：外部访问地址 / 对外端口（独立组件，走应用配置接口）—— */}
      <ExternalAccessSection />

      {/* —— 高级：TMDB 镜像地址（默认折叠）—— */}
      <section>
        <div className="css-glass !rounded-2xl">
          <div className="flex items-center gap-1.5 pr-5">
            <button
              type="button"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
              className="flex min-w-0 flex-1 items-center gap-2.5 px-5 py-4 text-left"
            >
              <ChevronRightIcon
                className={`size-4 shrink-0 text-[var(--text-faint)] transition-transform ${advancedOpen ? "rotate-90" : ""}`}
              />
              <span className="text-body font-medium text-[var(--text)]">TMDB 镜像地址</span>
              <span className="truncate text-caption text-[var(--text-faint)]">不走代理的替代方案</span>
            </button>
            <HelpDot
              content={
                <>
                  <p>
                    解决「TMDB 不可达」有两条独立的路：<strong>代理</strong>让流量绕行（访问地址不变）；
                    <strong>镜像</strong>把官方地址换成一个可直连的反代地址（流量不变、地址变了）。
                  </p>
                  <p className="mt-1.5">
                    有代理就不用配镜像，二选一即可。若两者都设置，请求会经代理去访问镜像地址。
                  </p>
                  <p className="mt-1.5 text-[var(--text-muted)]">
                    镜像可以是自建反代（nginx / Cloudflare Workers）或公共镜像；
                    注意公共镜像会经手你的 API Key，稳定性与隐私自行权衡。
                  </p>
                </>
              }
            />
          </div>
          {advancedOpen && (
            <div className="divide-y divide-white/[0.055] border-t border-white/[0.055]">
              {(
                [
                  [
                    "tmdb_api_base_url",
                    "接口地址",
                    "替换 api.themoviedb.org 的官方接口地址（发现页/搜索/订阅建档用）。",
                  ],
                  [
                    "tmdb_image_base_url",
                    "图床地址",
                    "替换 image.tmdb.org 的图床地址（海报/背景图回源用）。",
                  ],
                ] as const
              ).map(([field, label, help]) => (
                <div key={field} className="px-5 py-4">
                  <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-stretch max-md:gap-2">
                    <LabelWithHelp label={label} help={<p>{help} 留空使用默认值。</p>} />
                    <input
                      type="text"
                      defaultValue={form[field]}
                      onBlur={(e) => {
                        const value = e.target.value.trim();
                        if (value && !/^https?:\/\//.test(value)) {
                          setMirrorErrors((prev) => ({ ...prev, [field]: "需以 http(s):// 开头" }));
                          return;
                        }
                        setMirrorErrors((prev) => ({ ...prev, [field]: "" }));
                        commit({ ...form, [field]: value });
                      }}
                      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                      placeholder={view.mirror_defaults[field] ?? ""}
                      className="w-[340px] max-w-[60%] rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-sub text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50 max-md:w-full max-md:max-w-none"
                    />
                  </div>
                  {mirrorErrors[field] && (
                    <p className="mt-1.5 text-right text-caption text-red-300">{mirrorErrors[field]}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

/** 字段名 + ⓘ 帮助（说明文字一律收在 tooltip 里，页面只留字段本身）。 */
function LabelWithHelp({ label, help }: { label: string; help: React.ReactNode }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <span className="text-body font-medium text-[var(--text)]">{label}</span>
      <HelpDot content={help} />
    </span>
  );
}

/** ⓘ 小圆点：悬停/聚焦弹出说明。 */
function HelpDot({ content }: { content: React.ReactNode }) {
  return (
    <Tooltip content={content} placement="top" maxWidth={340}>
      <button
        type="button"
        aria-label="说明"
        className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
      >
        <InfoIcon className="size-[15px]" />
      </button>
    </Tooltip>
  );
}

/** 一行服务：名称 + ⓘ ｜ 测试结果 ｜ 测试 ｜ 开关。 */
function ServiceRow({
  label,
  description,
  backdrop,
  enabled,
  toggleDisabled,
  test,
  onTest,
  onToggle,
}: {
  label: string;
  description: string;
  /** 页面背景图：液态玻璃开关的采样来源 */
  backdrop: string;
  enabled: boolean;
  /** 无可用代理时禁用开关（拨了也不生效）；测试不受影响——直连也值得测 */
  toggleDisabled: boolean;
  test: TestState | undefined;
  onTest: () => void;
  onToggle: () => void;
}) {
  const pending = test?.state === "pending";
  const result = test?.state === "done" ? test.result : null;
  return (
    <div className="flex items-center gap-3 px-5 py-3.5">
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate text-body font-medium text-[var(--text)]">{label}</span>
        <HelpDot content={description} />
      </span>
      <span className="ml-auto flex items-center gap-3">
        {pending && <span className="text-sub text-[var(--text-faint)]">测试中…</span>}
        {result && (
          <Tooltip content={result.message} placement="top">
            <span
              className={`flex items-center gap-1.5 text-sub ${
                result.ok ? "text-emerald-300/90" : "text-red-300/90"
              }`}
            >
              <span
                className={`size-1.5 rounded-full ${result.ok ? "bg-[var(--ok)]" : "bg-[var(--danger)]"}`}
              />
              {result.ok ? (result.latency_ms !== null ? `连通 · ${result.latency_ms} ms` : "连通") : "不通"}
            </span>
          </Tooltip>
        )}
        <button
          type="button"
          onClick={onTest}
          disabled={pending}
          className="btn-glass px-3 py-1.5 text-sub font-medium disabled:opacity-40"
        >
          测试
        </button>
        {/* 与搜索/站点/下载器设置同款的受控 WebGL 液态玻璃开关 */}
        <LiquidGlassButton
          backgroundImage={backdrop}
          variant="dark"
          checked={enabled}
          disabled={toggleDisabled}
          aria-label={`${label} 走代理`}
          onCheckedChange={onToggle}
          className={`!min-h-0 !w-auto !gap-0 !bg-transparent !p-0 ${
            toggleDisabled ? "opacity-40" : ""
          }`}
        >
          <span className="sr-only">{enabled ? "走代理" : "直连"}</span>
        </LiquidGlassButton>
      </span>
    </div>
  );
}
