import { useEffect, useMemo, useRef, useState } from "react";
import { LiquidGlassRenderer } from "./core/LiquidGlassRenderer";
import {
  resolveLiquidGlassSettings,
  type LiquidGlassSettings,
  type LiquidGlassVariant
} from "./core/types";

export interface LiquidGlassIconButtonProps {
  active?: boolean;
  defaultActive?: boolean;
  disabled?: boolean;
  backgroundImage: string;
  variant?: LiquidGlassVariant;
  shape?: "circle" | "squircle" | "rounded" | "capsule";
  size?: "regular" | "compact";
  /** 是否创建 WebGL 玻璃渲染层（默认 true）。由宿主应用按主题能力传入
   *  （见 themes/<id>/theme.ts 的 capabilities.glass）——vendor 不感知主题名。 */
  glassEnabled?: boolean;
  /**
   * 显式指定按钮的像素尺寸（宽/高），覆盖 shape 内置的固定几何。
   * 组件的 WebGL 画布显示尺寸被 renderer 用内联样式锁定为几何尺寸，CSS 改不动；
   * 因此想要非默认大小（如工具栏里的小号发送键、侧栏整行 CTA）时，必须从这里传入，
   * 让画布几何与外层 CSS 盒子对齐。传了 width/height 时请同步用 className 设定同样大小。
   */
  width?: number;
  height?: number;
  settings?: Partial<LiquidGlassSettings>;
  onActiveChange?: (active: boolean) => void;
  className?: string;
  children?: React.ReactNode;
  "aria-label"?: string;
}

function FlashlightIcon() {
  return (
    <svg viewBox="0 0 32 48" aria-hidden="true">
      <path d="M7 2h18v5l-4 7v27c0 3-2 5-5 5s-5-2-5-5V14L7 7V2Z" fill="currentColor" />
      <path d="M7 8h18" fill="none" stroke="var(--lg-icon-cutout)" strokeWidth="2.6" />
      <circle cx="16" cy="29" r="3" fill="var(--lg-icon-cutout)" />
    </svg>
  );
}

export function LiquidGlassIconButton({
  active,
  defaultActive = false,
  disabled = false,
  backgroundImage,
  variant = "dark",
  shape = "circle",
  size = "regular",
  width,
  height,
  settings,
  glassEnabled = true,
  onActiveChange,
  className = "",
  children,
  "aria-label": ariaLabel = "Liquid glass action"
}: LiquidGlassIconButtonProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<LiquidGlassRenderer | null>(null);
  // glassEnabled = false 时不建 WebGL 渲染器（由宿主按主题能力传入，如
  // 纯色平铺主题：globals.css 的主题覆盖把玻璃控件接管为半透白实底按钮）。
  // 本组件与开关不同，渲染器**挂载即建、常驻到卸载**，不跳过的话每个图标
  // 按钮都独占一个 WebGL 上下文（浏览器每页上限约 16 个，见 LiquidGlassButton
  // 里 issue #89 的注释）。vendor 不感知主题名。
  const [internalActive, setInternalActive] = useState(defaultActive);
  const [pressed, setPressed] = useState(false);
  const currentActive = active ?? internalActive;
  const geometry = useMemo(() => {
    // 显式尺寸优先：镜片略小于盒子留出边缘高光，圆角按 shape 语义换算。
    if (width && height) {
      const min = Math.min(width, height);
      const radius =
        shape === "capsule" || shape === "circle"
          ? min / 2
          : shape === "squircle"
            ? min * 0.3
            : min * 0.22; // rounded
      return {
        width,
        height,
        lensWidth: Math.max(24, width - 8),
        lensHeight: Math.max(24, height - 8),
        radius
      };
    }
    if (shape === "capsule" && size === "compact") {
      return { width: 88, height: 40, lensWidth: 84, lensHeight: 36, radius: 18 };
    }
    if (shape === "capsule") return { width: 132, height: 72, lensWidth: 124, lensHeight: 64, radius: 32 };
    if (shape === "squircle") return { width: 96, height: 96, lensWidth: 88, lensHeight: 88, radius: 28 };
    if (shape === "rounded") return { width: 96, height: 96, lensWidth: 88, lensHeight: 88, radius: 20 };
    return { width: 96, height: 96, lensWidth: 88, lensHeight: 88, radius: 44 };
  }, [shape, size, width, height]);
  const mergedSettings = useMemo(() => resolveLiquidGlassSettings(variant, {
    depth: 46,
    blur: .22,
    refraction: .46,
    chromaticAberration: .035,
    edgeHighlight: .12,
    specular: .18,
    fresnel: 1.05,
    darkTint: .22,
    tintStrength: .08,
    opacity: 1,
    bevel: 0,
    ...settings,
    lensWidth: geometry.lensWidth,
    lensHeight: geometry.lensHeight,
    radius: geometry.radius
  }), [geometry, settings, variant]);

  const commit = () => {
    const next = !currentActive;
    if (active === undefined) setInternalActive(next);
    onActiveChange?.(next);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    // Netflix 主题：整体停用玻璃层（见上方 useTheme 处的注释），不创建渲染器；
    // theme.id 进依赖保证银玻璃 ⇄ Netflix 实时切换时旧渲染器被正确 dispose。
    if (!glassEnabled) return;
    try {
      const renderer = new LiquidGlassRenderer(canvas, backgroundImage, mergedSettings);
      renderer.setBackgroundSampling(true);
      renderer.resize(geometry.width, geometry.height);
      renderer.setTrack(-1000, -900, -1000, -950);
      renderer.setGeometry(geometry.width / 2, geometry.height / 2, 0, false, 1, 1, 0);
      rendererRef.current = renderer;
    } catch (error) {
      console.warn(error);
    }
    return () => {
      rendererRef.current?.dispose();
      rendererRef.current = null;
    };
  }, [geometry.height, geometry.width, glassEnabled]);

  useEffect(() => {
    rendererRef.current?.setImage(backgroundImage);
  }, [backgroundImage]);

  useEffect(() => {
    rendererRef.current?.setSettings(mergedSettings);
    rendererRef.current?.setGeometry(
      geometry.width / 2,
      geometry.height / 2,
      pressed ? .025 : 0,
      pressed,
      1,
      1,
      0
    );
  }, [geometry.height, geometry.width, mergedSettings, pressed]);

  return (
    <button
      type="button"
      className={`lg-icon-button lg-icon-button--${shape} lg-icon-button--${size} ${currentActive ? "is-active" : ""} ${pressed ? "is-pressed" : ""} ${className}`}
      aria-label={ariaLabel}
      aria-pressed={currentActive}
      disabled={disabled}
      onPointerDown={() => setPressed(true)}
      onPointerUp={() => setPressed(false)}
      onPointerCancel={() => setPressed(false)}
      onPointerLeave={() => setPressed(false)}
      onClick={commit}
    >
      <canvas ref={canvasRef} className="lg-icon-button__glass" aria-hidden="true" />
      <span className="lg-icon-button__active-surface" aria-hidden="true" />
      <span className="lg-icon-button__icon">{children ?? <FlashlightIcon />}</span>
    </button>
  );
}
