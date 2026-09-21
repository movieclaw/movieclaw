"use client";

import { useEffect, useState } from "react";

/**
 * 全屏沉浸展示位（发现页 Hero、详情页沉浸背景、媒体库 Billboard）是否值得
 * 加载 TMDB original 原图。
 *
 * 判据是物理像素：显示宽 × devicePixelRatio 超过 w1280 档的宽度（1280）才升清。
 * —— 393px×3 倍屏的手机物理宽只有 1179，w1280 已 1:1 覆盖，3840px 的原图是
 * 3~5 倍流量买零可见收益；2K/4K 桌面才是升清的目标场景（与后端刮削资产
 * tmdb_backdrop_size 默认 original 的取舍同一逻辑，见 core/config.py）。
 *
 * SSR/首帧恒为 false：升清本来就是挂载后的后台预加载（解码完成才换图），
 * 晚一个效果周期开始无感知；之后随窗口尺寸变化与跨屏拖动（DPR 随之变化）
 * 实时修正。
 */
export function useWantsOriginalImage(): boolean {
  const [wants, setWants] = useState(false);
  useEffect(() => {
    const update = () =>
      setWants(
        typeof window !== "undefined" &&
          window.innerWidth * (window.devicePixelRatio || 1) > 1280,
      );
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  return wants;
}
