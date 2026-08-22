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

// ---------------------------------------------------------------------------
// 界面偏好：按页面分组的样式设定（见 settings.schemas.UiPreferencesSetting）
// ---------------------------------------------------------------------------
// 全站样式设定集中在一个对象里，应用启动时拉一次（见 lib/ui-prefs.tsx）。
// 新增页面/设定时：这里加类型字段 + DEFAULT_UI_PREFS 加默认值，与后端模型对齐。

/** 侧边栏（液态玻璃面板）的样式偏好，参数含义见 lib/glass.ts。
 *  基底为 LiquidGlassCard 同款材质，三个值是在其上微调的滑杆。 */
export interface SidebarUiPrefs {
  /** 玻璃透明程度：0 Card 标准玻璃，1 玻璃完全隐去 */
  transparency: number;
  /** 玻璃明暗：-1 最暗 ~ 1 最亮，0 不加暗不提亮 */
  brightness: number;
  /** 玻璃厚度（边缘曲率带宽度，px）：10~90 */
  depth: number;
}

/** 全站背景蒙版（.page-scrim）的样式偏好，参数含义见 globals.css 的 .page-scrim。 */
export interface ScrimUiPrefs {
  /** 蒙版高斯模糊半径（px）：0 不模糊、背景清晰透出，越大背景越朦胧 */
  blur: number;
  /** 蒙版压暗程度：0 完全不压暗，1 全黑 */
  dark: number;
}

/** 侧栏主导航的个人排序。合并规则与"为什么只存顺序"见 lib/sidebar-nav.ts。 */
export interface NavUiPrefs {
  /** 导航项 id 的展示顺序；空数组 = 用内置默认顺序 */
  order: string[];
}

export interface UiPreferences {
  sidebar: SidebarUiPrefs;
  scrim: ScrimUiPrefs;
  nav: NavUiPrefs;
}

/** 各页面的默认样式（与后端模型默认值一致），拉取失败时前端以此兜底；
 *  设置页「恢复默认」也回到这一组值。改动时须同步后端
 *  SidebarUiPrefs / ScrimUiPrefs 与 globals.css 里 .page-scrim 的变量兜底值。 */
export const DEFAULT_UI_PREFS: UiPreferences = {
  sidebar: { transparency: 0.49, brightness: -0.36, depth: 28 },
  scrim: { blur: 13, dark: 0.69 },
  // 空顺序 = 内置默认顺序（导航项在 components/sidebar.tsx 的 SIDEBAR_NAV_ITEMS）
  nav: { order: [] },
};

/** 把后端返回的偏好与内置默认逐分组合并：老版本后端（不认识新分组/新字段）
 *  返回的数据会缺项，缺什么补什么的默认值，保证消费者拿到的结构永远完整。 */
function withDefaults(data: Partial<UiPreferences> | null | undefined): UiPreferences {
  return {
    sidebar: { ...DEFAULT_UI_PREFS.sidebar, ...data?.sidebar },
    scrim: { ...DEFAULT_UI_PREFS.scrim, ...data?.scrim },
    // order 必须兜住非数组：老后端不认识这个分组时返回的是 undefined，
    // 消费者（applyNavOrder）拿到的必须永远是可迭代的数组
    nav: { order: Array.isArray(data?.nav?.order) ? data.nav.order : [] },
  };
}

/** 读取全站界面偏好（从未配置的页面返回默认值），存服务端、跨设备一致。 */
export async function fetchUiPreferences(init?: RequestInit): Promise<UiPreferences> {
  return withDefaults(
    await unwrap(request<ApiEnvelope<Partial<UiPreferences>>>("/ui/preferences", init)),
  );
}

/** 整体覆盖式保存界面偏好，返回保存后的值。 */
export async function updateUiPreferences(prefs: UiPreferences): Promise<UiPreferences> {
  return withDefaults(
    await unwrap(
      request<ApiEnvelope<Partial<UiPreferences>>>("/ui/preferences", {
        method: "PUT",
        body: JSON.stringify(prefs),
      }),
    ),
  );
}
