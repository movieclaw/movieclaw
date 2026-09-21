"use client";

import { LiquidGlassButton as VendorLiquidGlassButton, type LiquidGlassButtonProps } from "@/vendor/liquid-glass/LiquidGlassButton";
import { LiquidGlassIconButton as VendorLiquidGlassIconButton, type LiquidGlassIconButtonProps } from "@/vendor/liquid-glass/LiquidGlassIconButton";
import { useResolvedTheme } from "@/themes/registry";

/**
 * 主题感知的液态玻璃控件（全站统一入口，等名包装 vendor）。
 *
 * 把主题的 capabilities.glass 翻译成 vendor 的 glassEnabled prop——vendor 只认
 * prop、不感知主题名。纯色平铺主题（capabilities.glass = false）下不创建 WebGL
 * 渲染层，外观由主题 CSS 覆盖接管（见 themes/netflix/tokens.css 的
 * .lg-button / .lg-icon-button 组）。
 *
 * 业务组件请从这里 import 玻璃控件；直接 import vendor 仅限不需要主题能力的场景。
 */
export function LiquidGlassButton(props: LiquidGlassButtonProps) {
  const { capabilities } = useResolvedTheme();
  return <VendorLiquidGlassButton {...props} glassEnabled={capabilities.glass} />;
}

export function LiquidGlassIconButton(props: LiquidGlassIconButtonProps) {
  const { capabilities } = useResolvedTheme();
  return <VendorLiquidGlassIconButton {...props} glassEnabled={capabilities.glass} />;
}
