import { useEffect, useMemo, useRef, useState } from "react";
import { LiquidGlassRenderer } from "./core/LiquidGlassRenderer";
import {
  resolveLiquidGlassSettings,
  type LiquidGlassSettings,
  type LiquidGlassVariant
} from "./core/types";

const MATERIAL_HANDOFF = 0.32;
const ANIMATION_TIME_SCALE = 1.5;
const TAP_TRANSITION_TIME_SCALE = 0.45;
const MORPH_SPRING_IN = 0.12;
const MORPH_SPRING_OUT = 0.145;
const MORPH_DAMPING_IN = 0.72;
const MORPH_DAMPING_OUT = 0.68;
const RELEASE_HANDOFF = MATERIAL_HANDOFF + 0.1;
const SWITCH_INSET = 3;
const GLASS_PAD = 32;
const scaledSpring = (value: number, timeScale = ANIMATION_TIME_SCALE) => value / (timeScale * timeScale);
const scaledRate = (value: number, timeScale = ANIMATION_TIME_SCALE) => Math.pow(value, 1 / timeScale);

export interface LiquidGlassButtonProps {
  checked?: boolean;
  defaultChecked?: boolean;
  disabled?: boolean;
  backgroundImage: string;
  variant?: LiquidGlassVariant;
  settings?: Partial<LiquidGlassSettings>;
  onCheckedChange?: (checked: boolean) => void;
  /**
   * 是否创建 WebGL 玻璃渲染层（默认 true）。由宿主应用按主题能力传入
   * （见 themes/<id>/theme.ts 的 capabilities.glass）——vendor 不感知主题名。
   * 关闭时静态层由主题 CSS 接管为实色形态（按下交接动画一并停用）。
   */
  glassEnabled?: boolean;
  className?: string;
  children?: React.ReactNode;
  "aria-label"?: string;
}

export function LiquidGlassButton({
  checked,
  defaultChecked = false,
  disabled = false,
  backgroundImage,
  variant = "dark",
  settings,
  onCheckedChange,
  glassEnabled = true,
  className = "",
  children,
  "aria-label": ariaLabel = "Liquid glass toggle"
}: LiquidGlassButtonProps) {
  const initialChecked = checked ?? defaultChecked;
  const rootRef = useRef<HTMLButtonElement>(null);
  const switchRef = useRef<HTMLSpanElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<LiquidGlassRenderer | null>(null);
  const frameRef = useRef(0);
  const resizeRef = useRef({ width: 0, height: 0 });
  const pointerRef = useRef({
    active: false,
    fastMorph: false,
    releasing: false,
    releaseRequested: false,
    releaseBounces: 0,
    previousDirection: 0,
    startChecked: initialChecked,
    startX: 0,
    moved: false,
    lastX: 0,
    lastTime: 0,
    stretch: 0,
    velocity: 0,
    target: 0,
    morph: 0,
    morphVelocity: 0,
    morphTarget: 0,
    position: initialChecked ? 1 : 0,
    positionVelocity: 0,
    positionTarget: initialChecked ? 1 : 0
  });
  const skipClickRef = useRef(false);
  const [internalChecked, setInternalChecked] = useState(defaultChecked);
  const [pressed, setPressed] = useState(false);
  const currentChecked = checked ?? internalChecked;
  const checkedRef = useRef(currentChecked);
  checkedRef.current = currentChecked;
  // glassEnabled = false 时整体停用玻璃层——不创建 WebGL 渲染器，静态层由
  // 主题 CSS 覆盖换成实色开关（灰轨道 + 红开启 + 白滑块）。注意玻璃层停用后
  // 按下动画里「静态层淡出让位玻璃」的交接不存在了，CSS 侧必须把静态层
  // opacity 钉住，否则按下的瞬间开关会整块透明。
  const mergedSettings = useMemo(() => resolveLiquidGlassSettings(variant, {
    blur: 0.18,
    refraction: 0.12,
    chromaticAberration: 0.045,
    distortion: 0.015,
    edgeHighlight: 0.08,
    specular: 0.14,
    fresnel: 1.08,
    radius: 22,
    depth: 42,
    brightness: -0.03,
    saturation: 0.03,
    darkTint: 0.28,
    tintStrength: 0.06,
    tint: 0,
    opacity: 1,
    lensWidth: 60,
    lensHeight: 38,
    liquidMotion: 0.14,
    liquidSpring: 0.055,
    liquidDamping: 0.84,
    bevel: 0,
    ...settings
  }), [settings, variant]);
  // 本项目改动（见下方 useEffect 的延后初始化）：renderer 可能在挂载之后才创建，
  // 那时 ensureRenderer 的闭包已经过期，必须从 ref 取最新配置，否则用到旧值。
  const settingsRef = useRef(mergedSettings);
  settingsRef.current = mergedSettings;

  const commit = (next: boolean) => {
    checkedRef.current = next;
    pointerRef.current.positionTarget = next ? 1 : 0;
    if (checked === undefined) setInternalChecked(next);
    onCheckedChange?.(next);
  };

  const ensureRenderer = () => {
    if (rendererRef.current || !canvasRef.current) return;
    // 玻璃停用：按下也不再建渲染器
    if (!glassEnabled) return;
    try {
      rendererRef.current = new LiquidGlassRenderer(canvasRef.current, backgroundImage, settingsRef.current);
      rendererRef.current.setBackgroundSampling(false);
      const { width, height } = resizeRef.current;
      if (width && height) rendererRef.current.resize(width + GLASS_PAD * 2, height + GLASS_PAD * 2);
    } catch (error) {
      console.warn(error);
    }
  };

  /**
   * 本项目改动：释放渲染器及其独占的 WebGL 上下文。开关只在按下/拖动的动画
   * 期间需要玻璃层（静止态玻璃 opacity 为 0，见 transitionState），动画归位后
   * 立刻归还上下文——浏览器每页只允许约 16 个活跃上下文，站点/搜索分类设置页
   * 一屏十几个开关若各自常驻一个，会把常驻的侧栏面板挤掉（表现为侧栏整块变白，
   * 见 issue #89）。下次按下时 ensureRenderer 会现场重建（约 35ms，可接受）。
   */
  const releaseRenderer = () => {
    rendererRef.current?.dispose();
    rendererRef.current = null;
  };

  const switchBounds = () => {
    const root = switchRef.current;
    if (!root) return { start: 0, end: 0 };
    const staticThumbHalfWidth = 18;
    const start = SWITCH_INSET + staticThumbHalfWidth;
    const end = root.clientWidth - SWITCH_INSET - staticThumbHalfWidth;
    return { start, end };
  };

  const thumbCenter = (checkedRatio: number) => {
    const root = switchRef.current;
    if (!root) return { x: 0, y: 0 };
    const bounds = switchBounds();
    return { x: bounds.start + (bounds.end - bounds.start) * checkedRatio, y: root.clientHeight / 2 };
  };

  const transitionState = (progress: number) => {
    const staticProgress = Math.min(1, progress / MATERIAL_HANDOFF);
    const glassProgress = Math.max(0, (progress - MATERIAL_HANDOFF) / (1 - MATERIAL_HANDOFF));
    const smoothStatic = staticProgress * staticProgress * (3 - 2 * staticProgress);
    const smoothGlass = glassProgress * glassProgress * (3 - 2 * glassProgress);
    return {
      geometryMorph: MATERIAL_HANDOFF * smoothStatic + (1 - MATERIAL_HANDOFF) * smoothGlass,
      materialMorph: smoothGlass,
      staticOpacity: 1 - smoothGlass,
      glassOpacity: smoothGlass
    };
  };

  const syncRendererGeometry = (stretch: number, active: boolean, progress: number) => {
    const root = rootRef.current;
    if (!root) return;
    const transition = transitionState(progress);
    const center = thumbCenter(pointerRef.current.position);
    const staticWidth = 36 + (mergedSettings.lensWidth - 36) * transition.geometryMorph;
    const staticHeight = 24 + (mergedSettings.lensHeight - 24) * transition.geometryMorph;
    root.style.setProperty("--lg-static-scale-x", String(staticWidth / 36));
    root.style.setProperty("--lg-static-scale-y", String(staticHeight / 24));
    root.style.setProperty("--lg-static-opacity", String(transition.staticOpacity));
    root.style.setProperty("--lg-glass-opacity", String(transition.glassOpacity));
    root.style.setProperty("--lg-thumb-x", `${22 * pointerRef.current.position}px`);
    const trackColor: [number, number, number] = checkedRef.current
      ? [0.33, 0.79, 0.34]
      : [0.34, 0.34, 0.37];
    rendererRef.current?.setTrackColors(trackColor, trackColor);
    rendererRef.current?.setTrack(
      GLASS_PAD + 20,
      GLASS_PAD + 44,
      GLASS_PAD + 16,
      GLASS_PAD + 44,
      10
    );
    rendererRef.current?.setGeometry(
      center.x + GLASS_PAD,
      center.y + GLASS_PAD,
      stretch,
      active,
      transition.geometryMorph,
      transition.materialMorph,
      1
    );
  };

  const updateFromPointer = (clientX: number) => {
    const root = switchRef.current;
    if (!root) return;
    const rect = root.getBoundingClientRect();
    const p = pointerRef.current;
    const bounds = switchBounds();
    const pointerX = clientX - rect.left;
    const travel = Math.max(1, bounds.end - bounds.start);
    p.position = Math.max(0, Math.min(1, (pointerX - bounds.start) / travel));
    p.positionVelocity = 0;
    const next = p.position >= .5;
    if (Math.abs(clientX - p.startX) > 2) p.moved = true;
    if (next !== checkedRef.current) {
      commit(next);
    }
    const now = performance.now();
    const elapsed = Math.max(8, now - p.lastTime);
    const speed = Math.abs(clientX - p.lastX) / elapsed * 1000;
    p.lastX = clientX;
    p.lastTime = now;
    p.target = Math.max(
      p.target * .72,
      .035 + Math.min(1, speed / 1600) * mergedSettings.liquidMotion
    );
    cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(animate);
  };

  const animate = () => {
    const p = pointerRef.current;
    p.velocity += (p.target - p.stretch) * scaledSpring(mergedSettings.liquidSpring);
    p.velocity *= scaledRate(p.active
      ? mergedSettings.liquidDamping
      : Math.min(.9, mergedSettings.liquidDamping + .025));
    p.stretch = Math.max(-.022, Math.min(.18, p.stretch + p.velocity));
    p.target *= scaledRate(p.active ? .93 : .78);

    if (!p.active) {
      p.positionVelocity += (p.positionTarget - p.position) * scaledSpring(MORPH_SPRING_IN);
      p.positionVelocity *= scaledRate(MORPH_DAMPING_IN);
      p.position = Math.max(0, Math.min(1, p.position + p.positionVelocity));
    }

    const morphSpring = p.morphTarget === 1 ? MORPH_SPRING_IN : MORPH_SPRING_OUT;
    const morphDamping = p.morphTarget === 1 ? MORPH_DAMPING_IN : MORPH_DAMPING_OUT;
    const morphTimeScale = p.fastMorph ? TAP_TRANSITION_TIME_SCALE : ANIMATION_TIME_SCALE;
    p.morphVelocity += (p.morphTarget - p.morph) * scaledSpring(morphSpring, morphTimeScale);
    p.morphVelocity *= scaledRate(morphDamping, morphTimeScale);
    p.morph = Math.max(0, Math.min(1, p.morph + p.morphVelocity));
    if (
      (p.morph === 0 && p.morphTarget === 0 && p.morphVelocity < 0) ||
      (p.morph === 1 && p.morphTarget === 1 && p.morphVelocity > 0)
    ) {
      p.morphVelocity = 0;
    }
    if (!p.active && p.releaseRequested && p.morphTarget === 1 && p.morph >= .97) {
      const tapPositionSettled = Math.abs(p.positionTarget - p.position) < .02;
      if (!p.fastMorph || tapPositionSettled) p.morphTarget = 0;
    }
    if (
      !p.active &&
      p.releaseRequested &&
      p.morphTarget > 0 &&
      p.morphTarget < 1 &&
      p.morph >= p.morphTarget - .01
    ) {
      p.morphTarget = 0;
    }

    if (p.releasing) {
      const direction = Math.sign(p.stretch);
      if (direction && p.previousDirection && direction !== p.previousDirection) {
        p.releaseBounces += 1;
      }
      if (direction) p.previousDirection = direction;
      if (p.releaseBounces >= 2) {
        p.stretch *= scaledRate(.72);
        p.velocity *= scaledRate(.48);
      }
    }

    syncRendererGeometry(p.stretch, p.active, p.morph);

    const shapeSettled = Math.abs(p.morphTarget - p.morph) + Math.abs(p.morphVelocity) < .002;
    const positionSettled = Math.abs(p.positionTarget - p.position) + Math.abs(p.positionVelocity) < .002;
    if (!p.active && p.releaseBounces >= 2 && Math.abs(p.stretch) + Math.abs(p.velocity) < .006 && shapeSettled && positionSettled) {
      p.stretch = 0;
      p.velocity = 0;
      p.target = 0;
      p.morph = 0;
      p.morphVelocity = 0;
      p.position = p.positionTarget;
      p.positionVelocity = 0;
      p.releasing = false;
      p.releaseRequested = false;
      p.fastMorph = false;
      syncRendererGeometry(0, false, 0);
      // 动画完全归位（玻璃层不可见）：释放 WebGL 上下文，见 releaseRenderer 说明
      releaseRenderer();
      return;
    }
    if (Math.abs(p.target) + Math.abs(p.stretch) + Math.abs(p.velocity) > .0015 || !shapeSettled || !positionSettled) {
      frameRef.current = requestAnimationFrame(animate);
    } else {
      p.stretch = 0;
      p.velocity = 0;
      p.target = 0;
      p.active = false;
      p.releasing = false;
      if (p.morph === 0) p.releaseRequested = false;
      if (p.morph === 0) p.fastMorph = false;
      p.morph = p.morphTarget;
      p.morphVelocity = 0;
      p.position = p.positionTarget;
      p.positionVelocity = 0;
      syncRendererGeometry(0, false, p.morph);
      // 玻璃层已完全淡出时释放 WebGL 上下文；morph 未归零说明玻璃还可见，保留
      if (p.morph === 0) releaseRenderer();
    }
  };

  useEffect(() => {
    if (!canvasRef.current || !switchRef.current) return;
    // —— 本项目改动：挂载时完全不创建 WebGL 渲染器 ——
    // 静止态下玻璃层的不透明度本就是 0（见 transitionState：未按下时只显示 CSS
    // 静态层），预创建没有视觉收益，却让每个开关常驻独占一个 WebGL 上下文。
    // 浏览器每页只允许约 16 个活跃上下文，站点/搜索分类设置页一屏十几个开关
    // 就会把常驻的侧栏玻璃面板挤掉（表现为侧栏整块变白，见 issue #89）。
    // 现在改为：按下时 ensureRenderer 现场创建（约 35ms，动画首帧内完成），
    // 动画归位后在 animate 的收尾分支里 releaseRenderer 归还上下文。
    syncRendererGeometry(pointerRef.current.stretch, false, pointerRef.current.morph);
    const root = switchRef.current;
    const observer = new ResizeObserver(([entry]) => {
      resizeRef.current = {
        width: entry.contentRect.width,
        height: entry.contentRect.height
      };
      rendererRef.current?.resize(
        entry.contentRect.width + GLASS_PAD * 2,
        entry.contentRect.height + GLASS_PAD * 2
      );
      syncRendererGeometry(pointerRef.current.stretch, pressed, pointerRef.current.morph);
    });
    observer.observe(root);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frameRef.current);
      releaseRenderer();
    };
  }, []);

  useEffect(() => {
    rendererRef.current?.setImage(backgroundImage);
  }, [backgroundImage]);

  useEffect(() => {
    rendererRef.current?.setSettings(mergedSettings);
    syncRendererGeometry(pointerRef.current.stretch, pressed, pointerRef.current.morph);
  }, [mergedSettings, pressed]);

  useEffect(() => {
    const p = pointerRef.current;
    p.positionTarget = currentChecked ? 1 : 0;
    if (!p.active) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = requestAnimationFrame(animate);
    }
  }, [currentChecked]);

  return (
    <button
      ref={rootRef}
      type="button"
      className={`lg-button ${currentChecked ? "is-checked" : ""} ${pressed ? "is-pressed" : ""} ${disabled ? "is-disabled" : ""} ${className}`}
      role="switch"
      aria-checked={currentChecked}
      aria-label={ariaLabel}
      disabled={disabled}
      onPointerDown={(event) => {
        if (disabled) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        skipClickRef.current = true;
        const p = pointerRef.current;
        p.active = true;
        p.fastMorph = false;
        p.releasing = false;
        p.releaseRequested = false;
        p.releaseBounces = 0;
        p.previousDirection = Math.sign(p.stretch);
        p.startChecked = currentChecked;
        p.startX = event.clientX;
        p.moved = false;
        p.lastX = event.clientX;
        p.lastTime = performance.now();
        p.target = .055;
        p.morphTarget = 1;
        setPressed(true);
        ensureRenderer();
        cancelAnimationFrame(frameRef.current);
        frameRef.current = requestAnimationFrame(animate);
      }}
      onPointerMove={(event) => {
        if (!pointerRef.current.active || disabled) return;
        updateFromPointer(event.clientX);
      }}
      onPointerUp={(event) => {
        if (disabled) return;
        const p = pointerRef.current;
        if (!p.moved) {
          commit(!p.startChecked);
          p.fastMorph = true;
          p.morph = Math.max(p.morph, .82);
          p.morphVelocity = Math.max(0, p.morphVelocity);
        }
        p.positionTarget = checkedRef.current ? 1 : 0;
        p.active = false;
        p.releasing = true;
        p.releaseRequested = true;
        p.releaseBounces = 0;
        p.previousDirection = Math.sign(p.stretch);
        p.target = 0;
        p.morphTarget = p.fastMorph
          ? 1
          : p.morph >= MATERIAL_HANDOFF ? 0 : RELEASE_HANDOFF;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
        setPressed(false);
        cancelAnimationFrame(frameRef.current);
        frameRef.current = requestAnimationFrame(animate);
      }}
      onPointerCancel={() => {
        if (disabled) return;
        const p = pointerRef.current;
        commit(p.startChecked);
        p.positionTarget = p.startChecked ? 1 : 0;
        p.active = false;
        p.releasing = true;
        p.releaseRequested = true;
        p.target = 0;
        p.morphTarget = p.morph >= MATERIAL_HANDOFF ? 0 : RELEASE_HANDOFF;
        setPressed(false);
        cancelAnimationFrame(frameRef.current);
        frameRef.current = requestAnimationFrame(animate);
      }}
      onPointerLeave={() => {
        if (disabled) return;
        setPressed(false);
      }}
      onClick={() => {
        if (disabled) return;
        if (skipClickRef.current) {
          skipClickRef.current = false;
          return;
        }
        const p = pointerRef.current;
        p.active = true;
        p.fastMorph = true;
        p.releasing = false;
        p.releaseRequested = false;
        p.releaseBounces = 0;
        p.startChecked = currentChecked;
        p.moved = false;
        p.target = .055;
        p.morphTarget = 1;
        setPressed(true);
        ensureRenderer();
        cancelAnimationFrame(frameRef.current);
        frameRef.current = requestAnimationFrame(() => {
          commit(!p.startChecked);
          p.morph = Math.max(p.morph, .82);
          p.morphVelocity = Math.max(0, p.morphVelocity);
          p.active = false;
          p.releasing = true;
          p.releaseRequested = true;
          p.morphTarget = 1;
          setPressed(false);
          animate();
        });
      }}
    >
      <span className="lg-button__label">{children}</span>
      <span ref={switchRef} className="lg-button__switch" aria-hidden="true">
        <canvas ref={canvasRef} className="lg-button__glass" aria-hidden="true" />
        <span className="lg-button__track" />
        <span className="lg-button__thumb" />
      </span>
    </button>
  );
}
