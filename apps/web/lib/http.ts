import { publicEnv } from "@/lib/env";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";

export class HttpError extends Error {
  status: number;
  details: unknown;

  constructor(message: string, status: number, details: unknown) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.details = details;
  }
}

function trimTrailingSlash(value: string): string {
  return value !== "/" && value.endsWith("/") ? value.slice(0, -1) : value;
}

function trimLeadingSlash(value: string): string {
  return value.startsWith("/") ? value.slice(1) : value;
}

/** 把 API 相对路径解析成完整请求地址（流式请求等不走 request() 的场景也复用）。 */
export function resolveRequestUrl(path: string): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }

  const baseUrl = trimTrailingSlash(publicEnv.apiBaseUrl);
  const requestPath = trimLeadingSlash(path);

  if (baseUrl === "/") {
    return `/${requestPath}`;
  }

  return `${baseUrl}/${requestPath}`;
}

function buildHeaders(initHeaders?: HeadersInit, body?: BodyInit | null): HeadersInit {
  const headers = new Headers(initHeaders);
  headers.set("Accept", "application/json");

  // FormData（文件上传）必须由浏览器自动带上含 boundary 的 multipart Content-Type，
  // 这里绝不能手动设 application/json，否则后端无法解析上传体。
  if (body && !(body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  return headers;
}

/**
 * 全站统一的未登录兜底：任何接口返回 401 就跳登录页。
 * /login、/setup 自身除外——登录失败（密码错误也是 401）要留在原页面展示错误。
 * 跳转时把当前地址（路径 + 查询串）编码进 ?next=，登录成功后原样回到用户离开的页面，
 * 避免会话过期后一律被打回首页。注意这只是体验优化，真正的安全边界在后端。
 */
export function redirectToLoginOn401(status: number): void {
  if (status === 401 && typeof window !== "undefined") {
    const path = window.location.pathname;
    // 影片分享页（/s/…）的访客没有账号：401 是「要密码」，由分享页自己接住
    if (path !== "/login" && path !== "/setup" && !path.startsWith("/s/")) {
      const next = encodeURIComponent(path + window.location.search);
      clearBackdropCache();
      clearUiPrefsCache();
      window.location.href = `/login?next=${next}`;
    }
  }
}

/**
 * 请求被浏览器自己掐断时的兜底文案。
 *
 * fetch 只在**网络层**失败时抛 TypeError，浏览器各写各的话术：WebKit 是
 * `Load failed`、Chrome 是 `Failed to fetch`、Firefox 是 `NetworkError ...`。
 * 原样甩给用户等于让他对着一句英文猜是文件坏了还是网断了（issue #432 里
 * iPhone Safari 就是这样显示的）。这里统一换成能行动的中文。
 */
const NETWORK_ERROR_MESSAGE = "网络中断或请求被浏览器放弃，请检查连接后重试";
const TIMEOUT_ERROR_MESSAGE = "服务器响应太慢，请求已取消——请稍后重试";

export interface RequestOptions {
  /**
   * 本次请求的超时毫秒数；不传 = 不设超时（沿用浏览器默认）。
   *
   * **只给明确知道该多快返回的接口用**。整站统一超时是陷阱：一键校准要解码
   * 音轨、上传要传完文件，给它们套一个数字只会把能成功的请求打断。
   */
  timeoutMs?: number;
}

/** 把调用方的 signal 与超时合成一个：AbortSignal.any 在旧 WebView 上没有。 */
function withTimeout(
  signal: AbortSignal | null | undefined,
  timeoutMs: number,
): { signal: AbortSignal; done: () => void; timedOut: () => boolean } {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const forward = () => controller.abort();
  if (signal) {
    if (signal.aborted) forward();
    else signal.addEventListener("abort", forward, { once: true });
  }
  return {
    signal: controller.signal,
    done: () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", forward);
    },
    timedOut: () => timedOut,
  };
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const timeout =
    options.timeoutMs !== undefined ? withTimeout(init.signal, options.timeoutMs) : null;

  // 整段（连同读响应体）都在 try 里：超时要能覆盖「连上了但一直不给完数据」，
  // 只掐 fetch 那一步等于只保护到响应头。
  try {
    const response = await fetch(resolveRequestUrl(path), {
      ...init,
      signal: timeout ? timeout.signal : init.signal,
      headers: buildHeaders(init.headers, init.body),
    });

    if (response.status === 204) {
      return undefined as T;
    }

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    const payload = isJson ? await response.json() : await response.text();

    if (!response.ok) {
      const message =
        isJson && payload && typeof payload === "object" && "message" in payload
          ? String(payload.message)
          : `Request failed with status ${response.status}`;

      redirectToLoginOn401(response.status);

      throw new HttpError(message, response.status, payload);
    }

    return payload as T;
  } catch (error) {
    // 后端给出的业务错误已经是可读中文，原样上抛
    if (error instanceof HttpError) throw error;
    // 调用方主动取消（换轨、关弹窗、组件卸载）也原样上抛：上层靠
    // signal.aborted / AbortError 区分「用户不要了」和「真出错了」。
    // 不写 instanceof DOMException——各运行时（浏览器 / undici）实现不一。
    if ((error as { name?: string } | null)?.name === "AbortError") {
      if (timeout?.timedOut()) {
        throw new HttpError(TIMEOUT_ERROR_MESSAGE, 0, null);
      }
      throw error;
    }
    if (error instanceof TypeError) {
      throw new HttpError(NETWORK_ERROR_MESSAGE, 0, null);
    }
    throw error;
  } finally {
    timeout?.done();
  }
}
