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
