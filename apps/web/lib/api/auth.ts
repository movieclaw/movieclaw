import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** 能力开关快照：前端据此裁剪入口；安全边界仍在后端 403。 */
export interface SessionCapabilities {
  allow_subscribe: boolean;
  allow_search: boolean;
  allow_direct_download: boolean;
}

/** 当前登录会话（见 schemas.auth.SessionView）。 */
export interface SessionView {
  username: string;
  /** 展示昵称；建号时默认取用户名，可在「个人信息」里修改 */
  nickname: string;
  /** 头像相对 URL（含版本号，换头像后 URL 变化以绕开缓存）；未上传过为空 */
  avatar_url: string | null;
  /** admin=超级管理员；member=成员（导航与设置分区按此裁剪） */
  role: "admin" | "member";
  /** 能力开关快照；管理员恒为全开 */
  capabilities: SessionCapabilities;
}

/** 首次初始化状态：未初始化时前端应进 /setup 引导页。 */
export interface BootstrapStatus {
  initialized: boolean;
}

/** 查询系统是否已完成首次初始化（公开接口）。 */
export function getBootstrapStatus(): Promise<BootstrapStatus> {
  return unwrap(request<ApiEnvelope<BootstrapStatus>>("/auth/bootstrap"));
}

/**
 * 首次初始化：创建超级管理员并自动登录（会话 Cookie 由后端种下）。
 * 服务端持有一次性锁：管理员已存在时返回 409，本调用会抛 HttpError。
 */
export function createAdmin(username: string, password: string): Promise<SessionView> {
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/bootstrap", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  );
}

/** 管理员登录。remember 为 true 时会话有效期 7 天 → 30 天。 */
export function login(
  username: string,
  password: string,
  remember: boolean,
): Promise<SessionView> {
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password, remember }),
    }),
  );
}

/**
 * 退出登录（会话已过期时调用也不会报错）。
 * 默认只退当前账号：浏览器里还有别的账号就自动切过去并返回它；返回 null 表示
 * 已没有任何账号。all=true 退出全部账号。
 */
export function logout(all = false): Promise<SessionView | null> {
  return unwrap(
    request<ApiEnvelope<SessionView | null>>("/auth/logout", {
      method: "POST",
      body: JSON.stringify({ all }),
    }),
  );
}

// ---------------------------------------------------------------------------
// 多账号切换（docs/design/account-switching.md）：浏览器同时保存多个账号的
// 登录态，切换不需要再输密码。凭证全部在 HttpOnly Cookie 里，前端只拿列表。
// ---------------------------------------------------------------------------

/** 浏览器已登录账号数上限（与后端 MAX_SAVED_ACCOUNTS 一致），满员时隐藏"添加账号"。 */
export const MAX_SAVED_ACCOUNTS = 5;

/** 浏览器当前持有的一个账号（见 schemas.auth.AccountView）。 */
export interface AccountView {
  username: string;
  nickname: string;
  /** 头像相对 URL（带 account 参数，非激活账号的头像也能读到）；未上传过为空 */
  avatar_url: string | null;
  role: "admin" | "member";
  /** 是否为当前激活账号；列表里恰有一个为 true，且排在第一 */
  active: boolean;
}

/** 列出本浏览器已登录的全部账号（激活账号排第一）。 */
export function listAccounts(): Promise<AccountView[]> {
  return unwrap(request<ApiEnvelope<AccountView[]>>("/auth/accounts"));
}

/** 切换到已登录的另一个账号。目标登录态已失效时抛 404，需重新登录该账号。 */
export function switchAccount(username: string): Promise<SessionView> {
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/accounts/switch", {
      method: "POST",
      body: JSON.stringify({ username }),
    }),
  );
}

/** 从本浏览器移除一个账号。返回体语义与 logout 相同：移除后所处的账号，null 表示已全部退出。 */
export function removeAccount(username: string): Promise<SessionView | null> {
  return unwrap(
    request<ApiEnvelope<SessionView | null>>(
      `/auth/accounts/${encodeURIComponent(username)}`,
      { method: "DELETE" },
    ),
  );
}

/** 查询当前登录状态；未登录时抛 401（由 http.ts 统一跳转登录页）。 */
export function getSession(): Promise<SessionView> {
  return unwrap(request<ApiEnvelope<SessionView>>("/auth/me"));
}

/** 修改展示昵称（登录用户名不可改）。 */
export function updateProfile(nickname: string): Promise<SessionView> {
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/profile", {
      method: "PUT",
      body: JSON.stringify({ nickname }),
    }),
  );
}

/** 上传（替换）头像。file 为已压缩的图片 Blob（通常是 JPEG）。 */
export function uploadAvatar(file: Blob): Promise<SessionView> {
  const form = new FormData();
  form.append("file", file, "avatar.jpg");
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/avatar", {
      method: "POST",
      body: form,
    }),
  );
}

/** 修改管理员密码：其余设备的会话全部强制下线，本会话自动续期。 */
export function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<SessionView> {
  return unwrap(
    request<ApiEnvelope<SessionView>>("/auth/password", {
      method: "PUT",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  );
}
