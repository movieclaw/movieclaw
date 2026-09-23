/**
 * 液态指示器的弹簧运动学（纯函数，无 DOM 依赖，test/liquid-spring.test.mjs 覆盖）。
 *
 * 用途：底栏选中胶囊在页签间「滑过去」的那一下（components/glass-tab-bar.tsx）。
 * 运动模型移植自 vendor/liquid-glass 的 LiquidGlassTabBar——那套 WebGL 组件的
 * 材质不适合底栏（只能折射静态背景图，盖在海报上是一块不透明黑胶囊，
 * 见 docs/design/web-themes-mobile/04 §3.5 的对比），但它的指示器手感是对的：
 *   - 位置走欠阻尼弹簧：到位时轻微越过再回弹，像一滴有惯性的液体；
 *   - 形状随速度拉伸：跑得越快横向拉得越长、纵向略收（体积守恒的错觉）；
 *   - 行进中「抬起」：整体放大一点，停稳后落回——iOS 26 选中气泡被拖动时的样子。
 *
 * 为什么预计算关键帧而不是每帧 JS 写 transform：交给 Web Animations API 播放，
 * 动画跑在合成线程上，主线程忙（路由切换正在渲染新页面）时也不掉帧；可被
 * 打断——新目标到来时按已播放时长查出当前位置与速度，从那里接着算。
 */

export interface SpringOptions {
  /** 刚度（越大越快） */
  stiffness?: number;
  /** 阻尼（越小回弹越明显） */
  damping?: number;
  /** 模拟步长（秒），默认 1/60 */
  dt?: number;
}

export interface SpringFrame {
  /** 距开始的时间（毫秒） */
  t: number;
  /** 位置（px） */
  x: number;
  /** 速度（px/s） */
  v: number;
}

/** 默认手感：约 0.45s 到位、过冲约 4%（按 vendor TabBar 的观感调出） */
export const INDICATOR_SPRING: Required<SpringOptions> = { stiffness: 320, damping: 25, dt: 1 / 60 };

/**
 * 从 from（初速度 v0）弹向 to，逐步积分直到静止，返回每一步的位置与速度。
 * 静止判定：离目标 < 0.3px 且速度 < 6px/s；最多模拟 2 秒兜底，末帧强制落在目标上。
 */
export function simulateSpring(
  from: number,
  to: number,
  v0 = 0,
  options: SpringOptions = {},
): SpringFrame[] {
  const { stiffness, damping, dt } = { ...INDICATOR_SPRING, ...options };
  const frames: SpringFrame[] = [{ t: 0, x: from, v: v0 }];
  let x = from;
  let v = v0;
  for (let step = 1; step <= Math.ceil(2 / dt); step++) {
    // 半隐式欧拉：先更新速度再更新位置，弹簧模拟的稳定写法
    v += (-stiffness * (x - to) - damping * v) * dt;
    x += v * dt;
    frames.push({ t: step * dt * 1000, x, v });
    if (Math.abs(x - to) < 0.3 && Math.abs(v) < 6) break;
  }
  const last = frames[frames.length - 1];
  frames[frames.length - 1] = { t: last.t, x: to, v: 0 };
  return frames;
}

/**
 * 速度 → 形变：横向拉伸系数（1 = 不拉伸），上限 1.3 防止拉成细线。
 * 标定：默认弹簧滑过一格（约 80px）峰值速度约 700px/s → 拉伸约 9%；
 * 跨三格约 2100px/s → 约 28%；手指快速拖拽再快也封顶在 30%。
 */
export function stretchForVelocity(v: number): number {
  return 1 + Math.min(Math.abs(v) / 7500, 0.3);
}

/**
 * 某一帧的 transform：平移 + 按速度拉伸（横长竖扁）+ 行进抬起。
 * lift 在 0..1 之间，由调用方按行程进度给（起步抬起、到位落下）。
 */
export function liquidTransform(x: number, v: number, lift: number): string {
  const sx = stretchForVelocity(v);
  const sy = 1 - (sx - 1) * 0.45;
  const scale = 1 + 0.12 * lift;
  return `translateX(${x.toFixed(2)}px) scale(${(sx * scale).toFixed(4)}, ${(sy * scale).toFixed(4)})`;
}

/**
 * 整段滑动的 WAAPI 关键帧：位置来自弹簧模拟，抬起量按正弦包络（起止为 0、
 * 行程中段最高），只在确实有位移时抬起——原地重播不应该鼓一下。
 */
export function liquidKeyframes(frames: SpringFrame[]): Keyframe[] {
  const total = frames[frames.length - 1].t || 1;
  const travel = Math.abs(frames[frames.length - 1].x - frames[0].x);
  const liftAmount = Math.min(travel / 60, 1);
  return frames.map((f) => ({
    offset: f.t / total,
    transform: liquidTransform(f.x, f.v, liftAmount * Math.sin(Math.PI * (f.t / total))),
  }));
}

/** 已播放 elapsed 毫秒时的状态（打断续算用）；超出范围取末帧 */
export function sampleAt(frames: SpringFrame[], elapsed: number): SpringFrame {
  if (elapsed <= 0) return frames[0];
  for (let i = 1; i < frames.length; i++) {
    if (frames[i].t >= elapsed) {
      const a = frames[i - 1];
      const b = frames[i];
      const k = (elapsed - a.t) / Math.max(1e-6, b.t - a.t);
      return { t: elapsed, x: a.x + (b.x - a.x) * k, v: a.v + (b.v - a.v) * k };
    }
  }
  return frames[frames.length - 1];
}
