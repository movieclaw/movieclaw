"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import * as appearanceApi from "@/lib/api/appearance";
import type { BackdropItem } from "@/lib/api/appearance";
import { BACKDROP_CACHE_KEY } from "@/lib/backdrop-cache";
import { BACKDROP } from "@/lib/glass";

/**
 * 背景图（backdrop）的全局状态。
 *
 * 站点背景大图有两处消费者，二者必须始终是同一张，折射才对得齐：
 *   1) CSS：body::before 铺满视口的大图（见 globals.css）；
 *   2) WebGL：每块液态玻璃面板都把这张图当作纹理采样、做边缘折射（见 glass-panel.tsx）。
 *
 * 因此"换背景图"不能只改 CSS——必须让上面两者同步切换。这里用一个 React Context
 * 作为唯一数据源：
 *   - 背景图存在**服务端**按账号隔离的「图库」里：用户上传的图全部保留、
 *     可点选切换，至多一张生效；同一账号跨设备保持一致，不同成员互不可见；
 *     生效图为空时用内置默认，Docker 重启也不丢；
 *   - 启动时向后端要图库与生效图，之后的每次写操作都以后端返回的最新视图回写状态；
 *   - 每次变更时把 CSS 变量 --backdrop-image 写到 <html> 上，body::before 立即换图；
 *   - 所有玻璃组件通过 useBackdrop() 拿到同一个 URL 传给着色器，随之重建纹理。
 *   - 后端返回的 URL 带版本号（?v=mtime），换图后 URL 变化，绕开浏览器与着色器的旧图缓存。
 */
interface BackdropContextValue {
  /** 当前生效的背景图 URL（内置路径或服务端图库地址） */
  backdrop: string;
  /** 是否为用户自定义（用于 UI 显示选中态等） */
  isCustom: boolean;
  /** 首次向后端拉取状态是否进行中（用于 UI 占位） */
  loading: boolean;
  /** 图库中的全部自定义背景图（上传时间升序） */
  items: BackdropItem[];
  /** 当前生效的图库图 id；null 表示内置默认 */
  activeId: string | null;
  /** 上传一张新图：压缩 → 存入服务端图库并设为生效 → 切换全站背景 */
  uploadBackdrop: (file: File) => Promise<void>;
  /** 点选切换生效图；传 null 切回内置默认（不删除任何图） */
  selectBackdrop: (backdropId: string | null) => Promise<void>;
  /** 从图库删除一张图；删的是生效图时自动回退内置默认 */
  deleteBackdrop: (backdropId: string) => Promise<void>;
  /**
   * 页面级临时背景覆盖（沉浸模式）：媒体库影片详情页进入时把全站背景
   * 换成该片剧照，离开时传 null 恢复用户自己的背景。只改运行时状态，
   * 不落外观设置、不写首帧缓存——刷新/下次启动仍是用户配置的背景。
   */
  setOverrideBackdrop: (url: string | null) => void;
}

const BackdropContext = createContext<BackdropContextValue | null>(null);

/** 把 URL 同步到 <html> 的 CSS 变量，供 body::before 使用；传 null 则回退默认。

同时把 URL 缓存进 localStorage：layout.tsx 的内联脚本会在下次刷新时于首帧
绘制前恢复这个变量，消除「先默认图、后自定义图」的背景闪烁（FOUC）。
缓存只是首帧优化，真实状态仍以每次启动拉取的 GET /appearance 为准——
缓存过期（图已删/已换）时首帧短暂显示旧图，接口返回后即纠正。 */
function applyCssVar(url: string | null) {
  const root = document.documentElement;
  if (url) {
    root.style.setProperty("--backdrop-image", `url("${url}")`);
  } else {
    root.style.removeProperty("--backdrop-image");
  }
  try {
    if (url) {
      localStorage.setItem(BACKDROP_CACHE_KEY, url);
    } else {
      localStorage.removeItem(BACKDROP_CACHE_KEY);
    }
  } catch {
    // localStorage 不可用（隐私模式等）只是失去首帧优化，不影响功能
  }
}

export function BackdropProvider({ children }: { children: React.ReactNode }) {
  // SSR 与首帧统一用内置图 + 空图库，避免水合不一致；挂载后再向后端要真实视图。
  const [activeUrl, setActiveUrl] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [items, setItems] = useState<BackdropItem[]>([]);
  const [loading, setLoading] = useState(true);
  // 页面级临时覆盖（影片详情的沉浸背景）：叠在用户配置之上，离开页面清除
  const [overrideUrl, setOverrideUrl] = useState<string | null>(null);
  // 已加载完成、正在覆盖层上展示的图。与 overrideUrl 分离是为了过渡体验：
  // 图先在内存里加载完才淡入（不铺半解码的图）；清除覆盖时保留这张图
  // 让覆盖层淡出，而不是图先消失再等透明度归零的"闪一下"
  const [overrideReady, setOverrideReady] = useState<string | null>(null);

  // 内部统一的回写：后端每次写操作都返回最新视图，整体应用并同步 CSS 变量。
  // 覆盖是独立的淡入图层，这里永远只管用户配置的背景（含首帧缓存）。
  const applyView = useCallback((view: appearanceApi.AppearanceView) => {
    setActiveUrl(view.active_url);
    setActiveId(view.active_id);
    setItems(view.backdrops);
    applyCssVar(view.active_url);
  }, []);

  // 覆盖图预加载：完整加载后才允许淡入
  useEffect(() => {
    if (overrideUrl === null) return; // 清除时不重置 ready——覆盖层带着旧图淡出
    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (!cancelled) setOverrideReady(overrideUrl);
    };
    img.src = overrideUrl;
    return () => {
      cancelled = true;
    };
  }, [overrideUrl]);

  // 覆盖层是否可见：目标图已加载完成才亮（首访图慢时，页面先以用户背景
  // 示人，图到了再柔和浮现——不再有"硬切"的突兀过渡）
  const overrideVisible = overrideUrl !== null && overrideReady === overrideUrl;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const view = await appearanceApi.getAppearance();
        if (!cancelled) applyView(view);
      } catch (err) {
        // 拉取失败（后端未起/网络问题）不致命：静默沿用内置默认背景。
        console.warn("读取外观设置失败，暂用内置默认背景：", err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyView]);

  const uploadBackdrop = useCallback(
    async (file: File) => {
      const blob = await fileToCompressedJpeg(file);
      applyView(await appearanceApi.uploadBackdrop(blob));
    },
    [applyView],
  );

  const selectBackdrop = useCallback(
    async (backdropId: string | null) => {
      applyView(await appearanceApi.setActiveBackdrop(backdropId));
    },
    [applyView],
  );

  const deleteBackdrop = useCallback(
    async (backdropId: string) => {
      applyView(await appearanceApi.deleteBackdrop(backdropId));
    },
    [applyView],
  );

  const value = useMemo<BackdropContextValue>(
    () => ({
      // 玻璃面板的折射纹理也走这里：与覆盖层淡入同一时机换图（图加载完成），
      // 折射内容和背景大图保持一致
      backdrop: (overrideVisible ? overrideUrl : activeUrl) ?? BACKDROP,
      isCustom: activeUrl != null,
      loading,
      items,
      activeId,
      uploadBackdrop,
      selectBackdrop,
      deleteBackdrop,
      setOverrideBackdrop: setOverrideUrl,
    }),
    [
      overrideVisible,
      overrideUrl,
      activeUrl,
      loading,
      items,
      activeId,
      uploadBackdrop,
      selectBackdrop,
      deleteBackdrop,
    ],
  );

  return (
    <BackdropContext.Provider value={value}>
      {/* 沉浸覆盖层：叠在 body::before(z0) 之上、全局蒙版(z5)与内容(z10)之下。
          独立图层 + 透明度过渡 = 图到了柔和浮现、离开页面柔和退场——
          背景大图的 CSS 变量与首帧缓存完全不被覆盖打扰。
          （opacity 由 state 驱动，不是 CSS 变量驱动，可以安全做 transition） */}
      <div
        aria-hidden="true"
        /* bottom 向下超出视口 --vp-overshoot：与 body::before 同样铺到屏幕物理
           底边，否则 iOS 独立 App 下底部会漏出一条底色（见 globals.css 的说明） */
        className="pointer-events-none fixed inset-0 z-[1] [bottom:calc(-1*var(--vp-overshoot))] transition-opacity duration-700 ease-out"
        style={{
          opacity: overrideVisible ? 1 : 0,
          backgroundImage: overrideReady ? `url("${overrideReady}")` : undefined,
          backgroundSize: "cover",
          // 与 body::before（globals.css）的 `center top / cover` 严格一致：
          // 两层铺的是同一块视口，锚点不同的话进出详情页会看到画面整体上下挪一下。
          // 改这里就要一起改那边
          backgroundPosition: "center top",
          backgroundRepeat: "no-repeat",
        }}
      />
      {children}
    </BackdropContext.Provider>
  );
}

/** 读取当前背景图与切换方法。必须在 BackdropProvider 内使用。 */
export function useBackdrop(): BackdropContextValue {
  const ctx = useContext(BackdropContext);
  if (!ctx) throw new Error("useBackdrop 必须在 <BackdropProvider> 内使用");
  return ctx;
}

/**
 * 把用户选择的图片文件读成一张「适度压缩」的 JPEG Blob。
 *
 * 为什么在前端压缩：背景图铺满视口不需要原始 4K/RAW 那么大，先把长边限制到 2560px
 * 并重编码为 JPEG，能大幅缩小上传体积、加快加载，也让服务端存储保持精简；
 * 同时把处理放在浏览器，服务端就无需引入图像库依赖。
 *
 * maxEdge 可按用途调小：头像等小图传 512 即可，进一步压缩上传体积。
 */
export function fileToCompressedJpeg(file: File, maxEdge = 2560): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith("image/")) {
      reject(new Error("请选择图片文件"));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("图片解码失败，请换一张试试"));
      img.onload = () => {
        const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          reject(new Error("当前浏览器不支持图片处理"));
          return;
        }
        ctx.drawImage(img, 0, 0, w, h);
        canvas.toBlob(
          (blob) => {
            if (blob) resolve(blob);
            else reject(new Error("图片编码失败，请重试"));
          },
          "image/jpeg",
          0.9,
        );
      };
      img.src = reader.result as string;
    };
    reader.readAsDataURL(file);
  });
}
