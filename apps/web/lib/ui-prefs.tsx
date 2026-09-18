"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  fetchUiPreferences,
  normalizeUiPreferences,
  updateUiPreferences,
  type UiPreferences,
} from "@/lib/api/ui";
import { DEFAULT_THEME_ID, normalizeThemeId, themeMeta, type ThemeMeta } from "@/lib/themes";
import { readUiPrefsCache, writeUiPrefsCache } from "@/lib/ui-prefs-cache";

/**
 * 界面偏好（按页面分组的样式设定）的全局状态。
 *
 * 与 backdrop / search-prefs 同款 Context 模式：应用启动时向后端拉**一次**
 * `ui.preferences` 整个配置域，之后全站（设置页、各业务页面）共享同一份状态
 * ——SPA 内切换页面不会重复请求；设置页改动即时保存并同步到所有消费者。
 *
 * 首帧不等接口：初始值取自 localStorage 缓存（上次拉到的偏好，见
 * lib/ui-prefs-cache.ts），接口返回后再以服务端值为准覆盖并回写缓存。
 * 没有这层缓存，自定义过导航顺序的用户每次刷新都会先看到默认排序、
 * 接口回来后再重排一次——这一跳是肉眼可见的（玻璃透明度、蒙版同理）。
 *
 * 扩展方式：后端配置域加字段 → lib/api/ui.ts 的类型与 DEFAULT_UI_PREFS 对齐
 * → 消费页面从 useUiPrefs().prefs.<页面> 取值。本文件无需再动。
 */
interface UiPrefsContextValue {
  /** 全站界面偏好（按页面分组）。存在预览草稿时返回草稿，实现「调节即生效」 */
  prefs: UiPreferences;
  /** 已保存（后端确认）的偏好，设置页用它判断草稿是否有未保存改动 */
  savedPrefs: UiPreferences;
  /** 首次向后端拉取是否进行中 */
  loading: boolean;
  /** 整体保存偏好；失败时状态回滚并抛错，由调用方展示错误 */
  savePrefs: (next: UiPreferences) => Promise<void>;
  /**
   * 设置未保存的预览草稿：全站消费者立即按草稿渲染（实时预览），
   * 传 null 撤销草稿、回到已保存值。仅设置页在调节期间使用。
   */
  setPreview: (draft: UiPreferences | null) => void;
}

const UiPrefsContext = createContext<UiPrefsContextValue | null>(null);

/**
 * 浏览器 UI 框（移动端地址栏 / PWA 状态栏）随主题取的画布色：与 <meta
 * name="theme-color"> 同步写入。manifest 的静态 theme_color 保持银玻璃
 * （只影响安装过渡帧，动态化需 cookie 路由，不值得）。
 */
const THEME_BROWSER_CHROME: Record<string, string> = {
  netflix: "#000000",
  [DEFAULT_THEME_ID]: "#0a0b10",
};

export function UiPrefsProvider({ children }: { children: React.ReactNode }) {
  // 惰性初始化读缓存：本 Provider 只在 AuthGate 确认登录后于客户端渲染
  // （见 components/app-shell.tsx），不参与 SSR，故可直接读 localStorage。
  // 缓存内容不可信，统一过一遍 normalizeUiPreferences 补齐缺项（无缓存时
  // 它补出来的就是 DEFAULT_UI_PREFS，与改动前的初始值完全一致）。
  const [prefs, setPrefs] = useState<UiPreferences>(() =>
    normalizeUiPreferences(readUiPrefsCache()),
  );
  const [preview, setPreview] = useState<UiPreferences | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchUiPreferences()
      .then((data) => {
        if (cancelled) return;
        setPrefs(data);
        // 服务端值即下次启动的首帧值
        writeUiPrefsCache(data);
      })
      .catch((err) => {
        // 拉取失败不致命：静默沿用首帧缓存里的偏好（没有缓存时即内置默认样式）
        console.warn("读取界面设置失败，暂用默认样式：", err);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  // 蒙版是全局 CSS（.page-scrim 的底色 + backdrop-filter），不像侧栏玻璃那样
  // 逐组件传参，因此这里把生效值（含预览草稿）写到 <html> 的 --scrim-blur /
  // --scrim-dark 变量上，全站蒙版即时跟随；调节滑杆时也走预览草稿，拖动即预览。
  const effectiveScrim = (preview ?? prefs).scrim;
  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty("--scrim-blur", `${effectiveScrim.blur}px`);
    root.style.setProperty("--scrim-dark", `${effectiveScrim.dark}`);
  }, [effectiveScrim.blur, effectiveScrim.dark]);

  // 主题同步：把生效主题（含设置页的预览草稿——点主题卡实时预览就靠它）写到
  // <html> 的 data-theme 属性上。token 层与圆角换档都挂在这个作用域，属性一改
  // 全站换肤；结构层（外壳分支）由 useTheme() 的消费方跟随同一份值渲染。
  // 默认主题移除属性而不是写 data-theme="silver"，与防闪烁脚本（只认 netflix）
  // 的落点保持一致，SSR 首屏也无需任何属性。
  const effectiveTheme = normalizeThemeId((preview ?? prefs).theme);
  useEffect(() => {
    const root = document.documentElement;
    if (effectiveTheme === DEFAULT_THEME_ID) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", effectiveTheme);
    // 浏览器 UI 框颜色与画布同源跟随（含设置页实时预览切主题的瞬间）。
    // Next 的路由元数据机制可能在客户端导航时把 viewport 导出的静态
    // themeColor（银玻璃值）写回 meta——用观察器持续断言当前主题的画布色，
    // 无论被谁改写都拉回，避免主题与浏览器框颜色脱节。
    const desired =
      THEME_BROWSER_CHROME[effectiveTheme] ?? THEME_BROWSER_CHROME[DEFAULT_THEME_ID];
    const apply = () => {
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta && meta.getAttribute("content") !== desired) {
        meta.setAttribute("content", desired);
      }
      return meta;
    };
    const meta = apply();
    const observer = new MutationObserver(apply);
    if (meta) observer.observe(meta, { attributes: true, attributeFilter: ["content"] });
    return () => observer.disconnect();
  }, [effectiveTheme]);

  const savePrefs = useCallback(
    async (next: UiPreferences) => {
      const previous = prefs;
      // 乐观更新：开关立即生效；保存失败回滚并抛给调用方提示
      setPrefs(next);
      try {
        const saved = await updateUiPreferences(next);
        setPrefs(saved);
        writeUiPrefsCache(saved);
        // 保存成功后草稿使命完成，撤销以免遮住刚落库的新值
        setPreview(null);
      } catch (err) {
        setPrefs(previous);
        throw err;
      }
    },
    [prefs],
  );

  const value = useMemo<UiPrefsContextValue>(
    () => ({ prefs: preview ?? prefs, savedPrefs: prefs, loading, savePrefs, setPreview }),
    [prefs, preview, loading, savePrefs],
  );

  return <UiPrefsContext.Provider value={value}>{children}</UiPrefsContext.Provider>;
}

/** 读取界面偏好与保存方法。必须在 UiPrefsProvider 内使用。 */
export function useUiPrefs(): UiPrefsContextValue {
  const ctx = useContext(UiPrefsContext);
  if (!ctx) throw new Error("useUiPrefs 必须在 <UiPrefsProvider> 内使用");
  return ctx;
}

/**
 * 读取当前主题（含设置页未保存的预览草稿）。
 *
 * 在 UiPrefsProvider 外调用（登录 / 初始化页的 GlassPanel 等前置页面）不抛错、
 * 按默认主题渲染：那些页面还没有账号上下文，主题本就未知；token 层不受影响
 * ——layout.tsx 的内联脚本已按 localStorage 缓存把 data-theme 写上 <html>，
 * 纯 CSS 换肤在任意页面都生效，这里兜底的只是结构层的分支选择。
 *
 * **只换皮肤（token 层）的消费方用它就够**；要按主题换路由或换整页结构的
 * 消费方必须改用 useThemeState()，原因见该函数说明。
 */
export function useTheme(): ThemeMeta {
  const ctx = useContext(UiPrefsContext);
  return themeMeta(ctx ? ctx.prefs.theme : DEFAULT_THEME_ID);
}

/**
 * 主题 + 「首次拉取是否还在进行中」。
 *
 * 结构层里凡是**会把人送走**的分支（/my 与 /settings 按主题 replace 到别的
 * 路由）都必须等 loading 落定再决策：首帧的主题取自 localStorage 缓存，
 * 冷缓存（新设备首次登录、无痕窗口、清过站点数据、别人发来的链接）时它
 * 返回的是默认主题 silver，于是 Netflix 用户点「我的」会被立刻 replace 走，
 * 等偏好到账时人已经在别的页面上了——刷新一次才正常，这个 bug 只在缓存
 * 冷的时候出现，非常难查。纯换肤的分支没有这个问题（渲染错一帧会自愈），
 * 换路由的分支不会自愈：路由已经跳了。
 */
export function useThemeState(): { theme: ThemeMeta; loading: boolean } {
  const ctx = useContext(UiPrefsContext);
  return {
    theme: themeMeta(ctx ? ctx.prefs.theme : DEFAULT_THEME_ID),
    // Provider 外（登录页等）没有偏好可等，直接按「已落定」处理
    loading: ctx ? ctx.loading : false,
  };
}
