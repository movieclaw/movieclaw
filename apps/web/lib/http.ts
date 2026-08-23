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
    if (path !== "/login" && path !== "/setup") {
      const next = encodeURIComponent(path + window.location.search);
      clearBackdropCache();
      clearUiPrefsCache();
      window.location.href = `/login?next=${next}`;
    }
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(resolveRequestUrl(path), {
    ...init,
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
}
