"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { BrandLoader } from "@/components/brand-loader";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";

import { getBootstrapStatus, getSession, type SessionView } from "@/lib/api/auth";
import { HttpError, resolveRequestUrl } from "@/lib/http";
import { SessionProvider } from "@/lib/session";
import { accessiblePathFor } from "@/lib/permissions";

interface StartupFailure {
  endpoint: string;
  message: string;
}

/**
 * 模块级会话缓存：本次页面加载内验证过一次就记下来。
 *
 * 为什么需要：工作台（(app) 组）与播放器（/play）是**两个 layout、各挂一个
 * AuthGate**。Next.js 跨 layout 导航会把旧 layout 整个卸掉、新的从零挂载——
 * 新 AuthGate 的 session 初值是 null，用户就会看到一屏「正在连接服务…」，等
 * bootstrap + me 两个串行请求跑完才见到播放器，观感像整页刷新了一次。点播放、
 * 退播放各来一回。
 *
 * 有缓存后：新挂载的 AuthGate 直接放行渲染，后台静默复验（权限被改、会话被
 * 收回时照样会被纠正——401 由 http.ts 整页跳登录）。
 *
 * 不会跨用户泄漏：登出与登录成功都是 window.location.href 整页跳转
 * （user-menu.tsx / login/page.tsx），模块状态随页面一起清零。
 */
let cachedSession: SessionView | null = null;

/**
 * 首页的鉴权门：在确认登录状态之前不渲染工作台，消除
 * "先闪一下主界面、再跳转登录/引导页"的割裂体验。
 *
 * 判定顺序（都是一次极轻的 GET）：
 * 1. 系统未初始化 → 直接去 /setup（不再经 /login 二连跳）；
 * 2. 已初始化但未登录 → getSession 得到 401，由 http.ts 统一跳 /login；
 * 3. 已登录 → 把会话数据注入 SessionProvider，放行渲染工作台。
 * 等待与失败状态使用不依赖业务 Provider 的轻量面板，API 不可达时仍可重试和自检。
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  // 初值取缓存：跨 layout 重挂载时立即放行渲染，不再闪「正在连接服务…」
  const [session, setSession] = useState<SessionView | null>(cachedSession);
  const [failure, setFailure] = useState<StartupFailure | null>(null);
  const [retryKey, setRetryKey] = useState(0);
  // 启动页的三段：on = 还在连接（本次是冷启动，没有缓存会话）；leaving = 已连上，
  // 工作台在底下挂好、启动页正在淡出；off = 撤掉。有缓存会话的重挂载直接 off。
  // 为什么连上后不立刻撤：工作台外壳的全局蒙版（.page-scrim）挂载时从透明渐显
  // 240ms，启动页一撤这段时间壁纸会裸露闪一下（2026-09-24 用户反馈）——启动页
  // 多停一拍、在蒙版就位后淡出，壁纸就始终盖在下面。
  const [splashPhase, setSplashPhase] = useState<"on" | "leaving" | "off">(
    cachedSession ? "off" : "on",
  );
  useEffect(() => {
    if (!session || splashPhase !== "on") return;
    setSplashPhase("leaving");
    const timer = setTimeout(() => setSplashPhase("off"), 520);
    return () => clearTimeout(timer);
  }, [session, splashPhase]);

  /** 改昵称等处更新会话时，缓存要一起跟上，否则下次跨 layout 会闪回旧数据 */
  const updateSession = useCallback((next: SessionView) => {
    cachedSession = next;
    setSession(next);
  }, []);

  useEffect(() => {
    let cancelled = false;
    // 缓存在手 = 系统必然已初始化，bootstrap 那一跳可以省掉
    const revalidating = cachedSession !== null;
    let endpoint = revalidating ? "/auth/me" : "/auth/bootstrap";
    setFailure(null);
    (async () => {
      try {
        if (!revalidating) {
          const status = await getBootstrapStatus();
          if (cancelled) return;
          if (!status.initialized) {
            router.replace("/setup");
            return;
          }
        }
        endpoint = "/auth/me";
        const view = await getSession(); // 未登录时抛 401，http.ts 拦截并整页跳 /login
        if (cancelled) return;
        cachedSession = view;
        const allowedPath = accessiblePathFor(view, pathname);
        if (allowedPath !== pathname) {
          router.replace(allowedPath as Route);
          return;
        }
        setSession(view);
      } catch (error) {
        if (cancelled) return;
        // 401 已由 http.ts 发起整页跳转；此处继续保持启动态，避免跳转前闪出错误卡。
        if (error instanceof HttpError && error.status === 401) return;
        console.error(`工作台启动失败（${resolveRequestUrl(endpoint)}）：`, error);
        // 静默复验失败（多半是瞬时网络问题）不打断已经在用的界面：
        // 界面自己的 API 调用会把持续性故障暴露出来
        if (revalidating) return;
        setFailure({ endpoint, message: startupFailureMessage(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, retryKey, router]);

  if (!session) {
    return (
      <StartupStatus
        failure={failure}
        onRetry={() => setRetryKey((value) => value + 1)}
      />
    );
  }
  // 缓存放行也不能放过权限：受限成员被丢进无权页面时，effect 里的 replace
  // 要一拍才生效，这里同步判掉，不让无权内容闪出一帧
  if (accessiblePathFor(session, pathname) !== pathname) {
    return <StartupStatus failure={null} onRetry={() => setRetryKey((value) => value + 1)} />;
  }
  return (
    <SessionProvider value={{ session, setSession: updateSession }}>
      {children}
      {splashPhase !== "off" && <SplashLayer leaving={splashPhase === "leaving"} />}
    </SessionProvider>
  );
}

/**
 * 启动页本体：与 iOS 启动图同色的实底 + 同一颗标（见 StartupStatus 的说明）。
 * ``leaving`` 时整层淡出（工作台已在底下），淡出期间不拦点击。
 * z 取 100：要压过外壳、底栏、顶栏与全部弹层，启动期间屏幕上只有它。
 */
function SplashLayer({ leaving = false }: { leaving?: boolean }) {
  return (
    <div
      aria-hidden="true"
      className={`fixed inset-0 z-[100] flex items-center justify-center bg-[var(--bg)] [bottom:calc(-1*var(--vp-overshoot))] transition-opacity duration-300 ease-out ${
        leaving ? "pointer-events-none opacity-0 delay-100" : "opacity-100"
      }`}
    >
      {/* 与 iOS 启动图同一颗标、同一尺寸档（启动图里约 94pt 宽），接管后原地呼吸 */}
      <BrandLoader className="size-24" />
    </div>
  );
}

/** 把启动异常收敛成部署用户能理解的中文信息；后端主动返回的业务消息原样保留。 */
function startupFailureMessage(error: unknown): string {
  if (!(error instanceof HttpError)) {
    return "无法连接到服务端，请检查 MovieClaw 是否正常运行以及当前访问地址。";
  }
  if (error.message === `Request failed with status ${error.status}`) {
    return `服务端请求失败（HTTP ${error.status}），请检查服务状态或反向代理配置。`;
  }
  return error.message;
}

/**
 * 工作台启动状态：一张与 iOS 启动图同构的启动页，不依赖 AppShell、用户偏好接口
 * 或 WebGL——即使故障正发生在这些初始化环节，用户仍能看到原因并自行重试。
 *
 * 为什么是「启动页」而不是原来的「正在连接服务…」小卡片：PWA 冷启动的顺序是
 * iOS 静态启动图（纯 --bg 底 + 居中 rotor 标，见 lib/apple-splash.ts）→ 本组件
 * → 工作台。原卡片压在**没有全局蒙版**的壁纸上（蒙版 .page-scrim 由 AppShell
 * 才渲染），前后两帧都是暗底、中间突然一屏亮壁纸加一张卡，像进错了应用
 * （2026-09-24 用户反馈）。现在本组件自己铺一层与启动图同色的实底、把同一颗
 * 标放在同一位置同一尺寸，从启动图接管后原地开始呼吸，连上就直接切进工作台，
 * 与原生 App「启动图 → 首屏」的体验一致。标随主题分叉由 BrandLoader 负责。
 * 铺底向下越出 --vp-overshoot：iOS 独立 App 的视口比屏幕矮一截（globals.css）。
 */
function StartupStatus({
  failure,
  onRetry,
}: {
  failure: StartupFailure | null;
  onRetry: () => void;
}) {
  return (
    <main
      role={failure ? "alert" : "status"}
      aria-live="polite"
      aria-label={failure ? "工作台加载失败" : "正在连接服务"}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-[var(--bg)] p-6 [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {!failure ? (
        <SplashLayer />
      ) : (
        <section
          // solid-popover：自绘登录门卡按浮层材质挂钩子（--line 描边 + #181818
          // 实底 + Netflix 投影 + 关 blur），圆角走 rounded-2xl 换档；银玻璃零变化
          className="solid-popover w-full max-w-[420px] rounded-2xl border border-white/[0.1] bg-[rgba(13,15,21,0.88)] p-6 shadow-2xl backdrop-blur-xl"
        >
          <p className="text-sub font-semibold uppercase tracking-[0.18em] text-[var(--text-faint)]">
            MovieClaw
          </p>
          <>
            <h1 className="mt-2 text-title font-semibold text-[var(--text)]">工作台加载失败</h1>
            {/* 部署出问题时这段报错是用户唯一能拿去求助的线索，PWA 下也必须可选可复制 */}
            <p className="selectable mt-2 text-ui leading-relaxed text-[var(--text-muted)]">
              {failure.message}
            </p>
            <p className="mt-3 break-all rounded-lg bg-black/25 px-3 py-2 font-mono text-caption text-[var(--text-faint)]">
              请求：{resolveRequestUrl(failure.endpoint)}
            </p>
            <div className="mt-5 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={onRetry}
                className="btn-accent h-9 rounded-full px-5 text-ui font-semibold"
              >
                重新连接
              </button>
              <Link
                href="/health"
                className="btn-glass flex h-9 items-center rounded-full px-5 text-ui font-medium"
              >
                查看系统状态
              </Link>
            </div>
          </>
        </section>
      )}
    </main>
  );
}
