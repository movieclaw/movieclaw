/**
 * 移动端底栏的 WebGL 液态玻璃边缘（折射 / 色散 / 菲涅尔 / 高光）。
 *
 * 为什么是「边缘」而不是整块玻璃：WebGL 读不到网页内容，而底栏浮在滚动的
 * 页面上，玻璃必须透出真实内容（布局不让位是定案，见
 * docs/design/web-themes-mobile/04 §3.7）。于是分两层：
 *   - 胶囊中心：CSS 层（现为清透、不模糊不染色），透出真实的滚动内容（含文字）；
 *   - 胶囊厚边（外沿向内 --thickness 一圈）：由本模块画。每帧把「底栏下方此刻
 *     看得见的东西」重建成一张场景纹理——站点背景大图（+ 全站蒙版）、页面里
 *     落在底栏下方的 <img>/<video>（海报、剧照）、底栏自己的滚动边缘压暗层——
 *     交给 studio 移植来的着色器（lib/glass-rim-shaders.ts）做折射与色散。
 *     场景里没有文字，所以厚边内缘渐隐回 CSS 中心，文字只在边上被「折」掉一点。
 *
 * 渲染管线：场景 2D canvas → 纹理 → 横 / 竖两遍高斯模糊（半分辨率）→ 主着色器
 * 输出到一块离屏 WebGL canvas → 按每个胶囊的位置 drawImage 到胶囊里自己的
 * 2D canvas。全站只占 1 个 WebGL 上下文（3 个胶囊各开一个会重复上传三遍纹理）。
 *
 * 只在「有东西在变」时出帧：滚动、胶囊过渡动画、按压、尺寸变化、图片加载、
 * 换背景都会续一段活跃期（ACTIVE_MS），期间逐帧重绘，之后停在最后一帧不耗电。
 */
import { BLUR_SHADER, MAIN_SHADER, VERTEX_SHADER } from "@/lib/glass-rim-shaders";

/** 光学参数：取自 liquid-glass-studio 默认值，按 54px 高的胶囊缩放了厚度与折射距离 */
export const RIM_OPTICS = {
  /** 厚边宽度（CSS px）：折射只发生在外沿向内这一圈 */
  thickness: 16,
  /** 外沿处的最大取样偏移（CSS px），决定透镜挤压的强弱 */
  refPx: 24,
  refFactor: 1.4,
  dispersion: 7,
  fresnelRange: 30,
  fresnelHardness: 0.2,
  fresnelFactor: 0.2,
  glareRange: 30,
  glareHardness: 0.2,
  glareConvergence: 0.5,
  glareOpposite: 0.8,
  glareFactor: 0.9,
  glareAngleDeg: -45,
  /** 厚边从这里（0~1，占厚度的比例）开始渐隐，交给 CSS 中心 */
  fadeStart: 0.55,
  /** 厚边也取模糊画面（studio 默认）：外沿若取清晰画面，会显得「只有边是透的」 */
  blurEdge: true,
};

/**
 * 与 CSS 中心同一套材质（globals.css 的 .glass-capsule[data-rim="on"]）：
 * 厚边内缘要和 CSS 中心无缝接上，各项必须两边一致，且按 CSS 的合成顺序运算：
 * 先 backdrop-filter（blur → saturate），再叠半透明底色（tint）。
 *
 * 暗色毛玻璃（2026-09-25 按用户给的 Instagram iOS 26 底栏截图实测）：整块均匀叠一层
 * 近黑冷灰——倒推「内 = 外 × (1 − α) + 染色 × α」得染色约 rgb(10,12,16)、α ≈ 0.78；
 * 模糊很强，底下只剩颜色倾向。这里 α 取 0.72（本站底栏下的页面本来就偏暗）、模糊 18px。
 * 与更早那版暗玻璃的区别：厚边也叠同一层染色与模糊，整块玻璃一个质地，折射 / 色散 /
 * 高光在暗色基底上照样可见（那版厚边清晰不染、中心又糊又暗，用户看成「只有边透明」）。
 * 同日否掉过的：全透明无模糊、按亮度自适应压暗、亮部压缩（contrast × brightness）。
 */
export const RIM_MATERIAL = {
  blurPx: 18,
  saturate: 1.5,
  tint: [12 / 255, 14 / 255, 18 / 255, 0.72] as [number, number, number, number],
};

/** 一次活跃期的时长：覆盖胶囊最长的弹簧过渡（640ms）再留余量 */
const ACTIVE_MS = 900;
/** 场景四周多取的边（CSS px）：模糊与折射都会读到胶囊外侧的像素 */
const SCENE_MARGIN = 28;

export interface RimTarget {
  /** 胶囊元素（圆角 = 高度一半） */
  capsule: HTMLElement;
  /** 胶囊里铺满的 canvas，边缘画在它上面 */
  canvas: HTMLCanvasElement;
}

type Program = { program: WebGLProgram; uniforms: Map<string, WebGLUniformLocation | null> };

function compile(gl: WebGL2RenderingContext, fragment: string): Program {
  const make = (type: number, source: string) => {
    const shader = gl.createShader(type)!;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(`液态玻璃着色器编译失败：${gl.getShaderInfoLog(shader)}`);
    }
    return shader;
  };
  const program = gl.createProgram()!;
  gl.attachShader(program, make(gl.VERTEX_SHADER, VERTEX_SHADER));
  gl.attachShader(program, make(gl.FRAGMENT_SHADER, fragment));
  gl.bindAttribLocation(program, 0, "a_pos");
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`液态玻璃着色器链接失败：${gl.getProgramInfoLog(program)}`);
  }
  return { program, uniforms: new Map() };
}

function uniform(gl: WebGL2RenderingContext, p: Program, name: string) {
  if (!p.uniforms.has(name)) p.uniforms.set(name, gl.getUniformLocation(p.program, name));
  return p.uniforms.get(name) ?? null;
}

function makeTexture(gl: WebGL2RenderingContext) {
  const tex = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return tex;
}

/** 从 CSS 的 url("…") 取出地址 */
function cssUrl(value: string): string | null {
  const m = value.match(/url\(\s*["']?([^"')]+)["']?\s*\)/);
  return m ? m[1] : null;
}

function sameOrigin(src: string): boolean {
  if (src.startsWith("data:") || src.startsWith("blob:")) return true;
  try {
    return new URL(src, location.href).origin === location.origin;
  } catch {
    return false;
  }
}

/** 页面媒体元素的静态样式缓存：裁切祖先与圆角在它的生命期里基本不变，只算一次 */
interface MediaInfo {
  fit: string;
  clip: HTMLElement;
  radius: number;
}

export class GlassRim {
  private readonly glCanvas = document.createElement("canvas");
  private readonly gl: WebGL2RenderingContext;
  private readonly blur: Program;
  private readonly main: Program;
  private readonly sceneTex: WebGLTexture;
  private readonly blurTex: [WebGLTexture, WebGLTexture];
  private readonly blurFbo: [WebGLFramebuffer, WebGLFramebuffer];
  private blurSize = [0, 0];

  private readonly scene = document.createElement("canvas");
  private readonly sceneCtx: CanvasRenderingContext2D;
  /** 固定不动的底：背景大图 + body::before 的光晕 / 压暗 + 全站蒙版，只在换图、换尺寸时重画 */
  private readonly floor = document.createElement("canvas");
  private floorKey = "";
  private floorScale = 1;
  private backdrop: HTMLImageElement | null = null;

  private readonly candidates = new Set<HTMLImageElement | HTMLVideoElement>();
  private readonly observed = new WeakSet<Element>();
  private readonly mediaInfo = new WeakMap<Element, MediaInfo>();
  private io: IntersectionObserver | null = null;
  private lastScan = 0;

  private targets: () => RimTarget[] = () => [];
  private activeUntil = 0;
  private raf = 0;
  private lost = false;
  private readonly cleanups: Array<() => void> = [];

  /** WebGL2 不可用时抛错，调用方保留纯 CSS 底栏 */
  constructor() {
    const gl = this.glCanvas.getContext("webgl2", {
      alpha: true,
      premultipliedAlpha: true,
      antialias: false,
      depth: false,
      stencil: false,
    });
    if (!gl) throw new Error("浏览器不支持 WebGL2，底栏保留纯 CSS 玻璃");
    this.gl = gl;
    const ctx = this.scene.getContext("2d", { alpha: false });
    if (!ctx) throw new Error("无法创建 2D 画布");
    this.sceneCtx = ctx;

    this.blur = compile(gl, BLUR_SHADER);
    this.main = compile(gl, MAIN_SHADER);

    const quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    this.sceneTex = makeTexture(gl);
    this.blurTex = [makeTexture(gl), makeTexture(gl)];
    this.blurFbo = [gl.createFramebuffer()!, gl.createFramebuffer()!];

    const onLost = (event: Event) => {
      event.preventDefault();
      this.lost = true;
      this.unmarkCapsules();
    };
    this.glCanvas.addEventListener("webglcontextlost", onLost);
    this.cleanups.push(() => this.glCanvas.removeEventListener("webglcontextlost", onLost));
  }

  /** 开始工作：挂事件、首帧绘制。targets 每帧调用，返回当前在场的胶囊 */
  start(targets: () => RimTarget[]) {
    this.targets = targets;
    const wake = () => this.wake();
    const onTransition = (event: Event) => {
      const t = event.target;
      if (t instanceof Element && t.closest(".glass-tabbar, .glass-tabbar-accessory, .glass-tabbar-edge")) wake();
    };
    document.addEventListener("scroll", wake, { capture: true, passive: true });
    document.addEventListener("load", wake, true);
    document.addEventListener("transitionrun", onTransition, true);
    document.addEventListener("transitionend", onTransition, true);
    document.addEventListener("pointerdown", wake, { capture: true, passive: true });
    document.addEventListener("pointerup", wake, { capture: true, passive: true });
    window.addEventListener("resize", wake);
    // 换背景图 = BackdropProvider 改 <html> 上的 --backdrop-image
    const mo = new MutationObserver(wake);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class"] });
    this.cleanups.push(() => {
      document.removeEventListener("scroll", wake, { capture: true });
      document.removeEventListener("load", wake, true);
      document.removeEventListener("transitionrun", onTransition, true);
      document.removeEventListener("transitionend", onTransition, true);
      document.removeEventListener("pointerdown", wake, { capture: true });
      document.removeEventListener("pointerup", wake, { capture: true });
      window.removeEventListener("resize", wake);
      mo.disconnect();
    });
    this.wake();
  }

  /** 续一段活跃期（路由切换等外部事件也调它） */
  wake() {
    if (this.lost) return;
    this.activeUntil = performance.now() + ACTIVE_MS;
    if (!this.raf) this.raf = requestAnimationFrame(this.tick);
  }

  dispose() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.cleanups.forEach((fn) => fn());
    this.io?.disconnect();
    this.unmarkCapsules();
    this.gl.getExtension("WEBGL_lose_context")?.loseContext();
  }

  /** 退回纯 CSS 玻璃：摘掉 data-rim，CSS 恢复原来的暗玻璃中心与描边 */
  private unmarkCapsules() {
    for (const { capsule } of this.targets()) delete capsule.dataset.rim;
  }

  private tick = (now: number) => {
    this.raf = 0;
    if (this.lost) return;
    try {
      this.render();
    } catch (error) {
      // 画不出来就退回纯 CSS 玻璃，不影响底栏可用
      console.error("液态玻璃底栏渲染失败，已退回 CSS 玻璃：", error);
      this.lost = true;
      this.unmarkCapsules();
      return;
    }
    if (now < this.activeUntil) this.raf = requestAnimationFrame(this.tick);
  };

  // ———— 场景采集 ————

  /**
   * 维护「视口内可见」的图片候选集（是否落在底栏下方由 drawScene 每帧按矩形筛）。
   * IntersectionObserver 管进出，每 400ms 补扫一次新挂上的图。观察范围刻意用整个
   * 视口、不带 rootMargin：rootMargin 依赖视口高度，iOS 工具栏伸缩一变就得重建
   * 观察器、候选集清空，回调补齐前的几帧厚边里只剩暗底（测试时实际闪过）。
   */
  private refreshCandidates() {
    if (!this.io) {
      this.io = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const el = entry.target as HTMLImageElement | HTMLVideoElement;
          if (entry.isIntersecting) this.candidates.add(el);
          else this.candidates.delete(el);
        }
        this.wake();
      });
    }
    const now = performance.now();
    if (now - this.lastScan < 400) return;
    this.lastScan = now;
    const vh = window.innerHeight;
    const fresh = new WeakSet<Element>();
    document.querySelectorAll<HTMLImageElement | HTMLVideoElement>(".app-shell main img, .app-shell main video").forEach((el) => {
      fresh.add(el);
      if (this.observed.has(el)) return;
      this.observed.add(el);
      this.io!.observe(el);
      // 观察器首次回调是异步的；新图当场判一次可见，换页后第一帧就能折射到它
      const r = el.getBoundingClientRect();
      if (r.bottom > 0 && r.top < vh && r.width > 0) this.candidates.add(el);
    });
    // 已离开 DOM 的元素移出候选
    for (const el of this.candidates) if (!el.isConnected || !fresh.has(el)) this.candidates.delete(el);
  }

  private infoOf(el: HTMLElement): MediaInfo {
    let info = this.mediaInfo.get(el);
    if (info) return info;
    const fit = getComputedStyle(el).objectFit || "fill";
    // 圆角裁切来自自身或最近几层带圆角的祖先（海报卡片通常是外层 overflow:hidden + rounded）
    let clip: HTMLElement = el;
    let radius = 0;
    let node: HTMLElement | null = el;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const r = parseFloat(getComputedStyle(node).borderTopLeftRadius) || 0;
      if (r > 0) {
        clip = node;
        radius = r;
        break;
      }
    }
    info = { fit, clip, radius };
    this.mediaInfo.set(el, info);
    return info;
  }

  private ensureFloor() {
    const root = getComputedStyle(document.documentElement);
    const url = cssUrl(root.getPropertyValue("--backdrop-image")) ?? "/backdrop-default.jpg";
    const overshoot = parseFloat(root.getPropertyValue("--vp-overshoot")) || 0;
    const scrim = document.querySelector<HTMLElement>(".page-scrim, .page-solid");
    const scrimColor = scrim ? getComputedStyle(scrim).backgroundColor : "";
    const vw = window.innerWidth;
    const vh = window.innerHeight + overshoot;
    const key = [url, vw, vh, scrimColor].join("|");
    if (key === this.floorKey && this.backdrop?.complete) return;

    if (!this.backdrop || this.backdrop.dataset.src !== url) {
      const img = new Image();
      img.dataset.src = url;
      img.decoding = "async";
      img.onload = () => this.wake();
      img.src = url;
      this.backdrop = img;
    }
    this.floorKey = key;
    // 有蒙版时背景本来就被糊掉（--scrim-blur 约 13px），低分辨率绘制再放大即近似模糊
    this.floorScale = scrimColor ? 0.125 : 0.5;
    const s = this.floorScale;
    this.floor.width = Math.max(1, Math.round(vw * s));
    this.floor.height = Math.max(1, Math.round(vh * s));
    const ctx = this.floor.getContext("2d")!;
    ctx.setTransform(s, 0, 0, s, 0, 0);
    ctx.fillStyle = "#07080d";
    ctx.fillRect(0, 0, vw, vh);
    const img = this.backdrop;
    if (img.complete && img.naturalWidth > 0 && sameOrigin(url)) {
      // body::before：cover + center top
      const k = Math.max(vw / img.naturalWidth, vh / img.naturalHeight);
      const w = img.naturalWidth * k;
      ctx.drawImage(img, (vw - w) / 2, 0, w, img.naturalHeight * k);
    }
    // body::before 叠在图上的光晕与竖向压暗（与 globals.css 同一组数值）
    const radial = (cx: number, cy: number, rx: number, ry: number, rgba: string, stop: number) => {
      ctx.save();
      ctx.translate(cx * vw, cy * vh);
      ctx.scale(rx * vw, ry * vh);
      const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
      g.addColorStop(0, rgba);
      g.addColorStop(stop, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(-2, -2, 4, 4);
      ctx.restore();
    };
    radial(0.16, 0.08, 0.58, 0.48, "rgba(96,130,220,0.18)", 0.62);
    radial(0.88, 0.14, 0.54, 0.44, "rgba(140,104,214,0.14)", 0.6);
    radial(0.8, 0.92, 0.64, 0.58, "rgba(64,150,186,0.13)", 0.64);
    const lin = ctx.createLinearGradient(0, 0, 0, vh);
    lin.addColorStop(0, "rgba(9,11,17,0.1)");
    lin.addColorStop(1, "rgba(6,7,13,0.32)");
    ctx.fillStyle = lin;
    ctx.fillRect(0, 0, vw, vh);
    if (scrimColor) {
      ctx.fillStyle = scrimColor;
      ctx.fillRect(0, 0, vw, vh);
    }
  }

  /** 画出 bbox（视口 CSS 坐标）范围内「底栏下方看得见的东西」 */
  private drawScene(bx: number, by: number, bw: number, bh: number, scale: number) {
    const ctx = this.sceneCtx;
    const w = Math.max(1, Math.round(bw * scale));
    const h = Math.max(1, Math.round(bh * scale));
    if (this.scene.width !== w || this.scene.height !== h) {
      this.scene.width = w;
      this.scene.height = h;
    }
    ctx.setTransform(scale, 0, 0, scale, -bx * scale, -by * scale);
    ctx.imageSmoothingEnabled = true;
    const fs = this.floorScale;
    ctx.drawImage(this.floor, bx * fs, by * fs, bw * fs, bh * fs, bx, by, bw, bh);

    for (const el of this.candidates) {
      const isImg = el instanceof HTMLImageElement;
      const nw = isImg ? el.naturalWidth : el.videoWidth;
      const nh = isImg ? el.naturalHeight : el.videoHeight;
      if (!nw || !nh || (isImg && !el.complete)) continue;
      if (!sameOrigin(isImg ? el.currentSrc || el.src : el.currentSrc)) continue;
      const r = el.getBoundingClientRect();
      if (r.right < bx || r.left > bx + bw || r.bottom < by || r.top > by + bh || r.width < 1) continue;
      const opacity = parseFloat(getComputedStyle(el).opacity);
      if (!(opacity > 0.01)) continue;
      const info = this.infoOf(el);
      const cr = info.clip === el ? r : info.clip.getBoundingClientRect();
      // object-fit 换算出源图取样区域（cover 居中裁切 / contain 留边 / 其余拉伸）
      let sx = 0, sy = 0, sw = nw, sh = nh;
      let dx = r.left, dy = r.top, dw = r.width, dh = r.height;
      if (info.fit === "cover") {
        const k = Math.max(r.width / nw, r.height / nh);
        sw = r.width / k;
        sh = r.height / k;
        sx = (nw - sw) / 2;
        sy = (nh - sh) / 2;
      } else if (info.fit === "contain") {
        const k = Math.min(r.width / nw, r.height / nh);
        dw = nw * k;
        dh = nh * k;
        dx = r.left + (r.width - dw) / 2;
        dy = r.top + (r.height - dh) / 2;
      }
      ctx.save();
      ctx.globalAlpha = opacity;
      ctx.beginPath();
      ctx.roundRect(cr.left, cr.top, cr.width, cr.height, info.radius);
      ctx.clip();
      ctx.drawImage(el, sx, sy, sw, sh, dx, dy, dw, dh);
      ctx.restore();
    }

    // 底栏自己的滚动边缘压暗层（.glass-tabbar-edge）：颜色渐变 × 遮罩渐变的乘积；
    // 清透玻璃下它被 CSS 隐藏，这里随之跳过
    const edge = document.querySelector<HTMLElement>(".glass-tabbar-edge");
    if (edge && getComputedStyle(edge).display !== "none") {
      const er = edge.getBoundingClientRect();
      const g = ctx.createLinearGradient(0, er.top, 0, er.bottom);
      g.addColorStop(0, "rgba(8,10,15,0)");
      g.addColorStop(0.29, "rgba(8,10,15,0.1)");
      g.addColorStop(0.58, "rgba(8,10,15,0.39)");
      g.addColorStop(0.62, "rgba(8,10,15,0.42)");
      g.addColorStop(1, "rgba(8,10,15,0.58)");
      ctx.fillStyle = g;
      ctx.fillRect(er.left, er.top, er.width, er.height);
    }
  }

  // ———— 渲染 ————

  private render() {
    const targets = this.targets().filter(({ capsule }) => capsule.isConnected);
    const shapes = targets
      .map((t) => ({ ...t, rect: t.capsule.getBoundingClientRect() }))
      .filter(({ rect, capsule }) => rect.width > 1 && rect.height > 1 && getComputedStyle(capsule).opacity !== "0")
      .slice(0, 3);
    if (!shapes.length) return;

    this.refreshCandidates();
    this.ensureFloor();

    const left = Math.min(...shapes.map((s) => s.rect.left)) - SCENE_MARGIN;
    const top = Math.min(...shapes.map((s) => s.rect.top)) - SCENE_MARGIN;
    const right = Math.max(...shapes.map((s) => s.rect.right)) + SCENE_MARGIN;
    const bottom = Math.max(...shapes.map((s) => s.rect.bottom)) + SCENE_MARGIN;
    const bw = right - left;
    const bh = bottom - top;
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    this.drawScene(left, top, bw, bh, scale);

    const gl = this.gl;
    const W = this.scene.width;
    const H = this.scene.height;
    if (this.glCanvas.width !== W || this.glCanvas.height !== H) {
      this.glCanvas.width = W;
      this.glCanvas.height = H;
    }

    gl.bindTexture(gl.TEXTURE_2D, this.sceneTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, this.scene);

    const blurred = RIM_MATERIAL.blurPx > 0;
    if (blurred) {
      // 半分辨率两遍高斯模糊：sigma 与 CSS 中心的 blur() 半径一致（CSS blur(r) 即 σ = r）
      const bwPx = Math.max(1, Math.round(bw / 2));
      const bhPx = Math.max(1, Math.round(bh / 2));
      if (this.blurSize[0] !== bwPx || this.blurSize[1] !== bhPx) {
        this.blurSize = [bwPx, bhPx];
        for (let i = 0; i < 2; i++) {
          gl.bindTexture(gl.TEXTURE_2D, this.blurTex[i]);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, bwPx, bhPx, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
          gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFbo[i]);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.blurTex[i], 0);
        }
      }
      gl.useProgram(this.blur.program);
      gl.uniform1i(uniform(gl, this.blur, "u_input"), 0);
      gl.uniform1f(uniform(gl, this.blur, "u_sigma"), RIM_MATERIAL.blurPx / 2);
      gl.viewport(0, 0, bwPx, bhPx);
      gl.activeTexture(gl.TEXTURE0);
      // 横向那遍直接从全分辨率场景取样，步长按半分辨率纹素折算（1 半分辨率纹素 = 2 全分辨率纹素）
      const passes: Array<[WebGLTexture, number, [number, number], [number, number]]> = [
        [this.sceneTex, 0, [2 / W, 2 / H], [1, 0]],
        [this.blurTex[0], 1, [1 / bwPx, 1 / bhPx], [0, 1]],
      ];
      for (const [input, out, texel, dir] of passes) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFbo[out]);
        gl.bindTexture(gl.TEXTURE_2D, input);
        gl.uniform2f(uniform(gl, this.blur, "u_texel"), texel[0], texel[1]);
        gl.uniform2f(uniform(gl, this.blur, "u_dir"), dir[0], dir[1]);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
    }

    // 主着色器 → 离屏 canvas
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const m = this.main;
    gl.useProgram(m.program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.sceneTex);
    gl.uniform1i(uniform(gl, m, "u_scene"), 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, blurred ? this.blurTex[1] : this.sceneTex);
    gl.uniform1i(uniform(gl, m, "u_blurred"), 1);
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform2f(uniform(gl, m, "u_res"), W, H);
    gl.uniform1f(uniform(gl, m, "u_scale"), W / bw);
    gl.uniform2f(uniform(gl, m, "u_origin"), left, top);
    gl.uniform2f(uniform(gl, m, "u_size"), bw, bh);
    gl.uniform1i(uniform(gl, m, "u_count"), shapes.length);
    const shapeData = new Float32Array(12);
    const radii = new Float32Array(3);
    shapes.forEach(({ rect }, i) => {
      shapeData.set([rect.left + rect.width / 2, rect.top + rect.height / 2, rect.width / 2, rect.height / 2], i * 4);
      radii[i] = Math.min(rect.width, rect.height) / 2;
    });
    gl.uniform4fv(uniform(gl, m, "u_shapes"), shapeData);
    gl.uniform1fv(uniform(gl, m, "u_radius"), radii);
    const o = RIM_OPTICS;
    const set1 = (name: string, v: number) => gl.uniform1f(uniform(gl, m, name), v);
    set1("u_thickness", o.thickness);
    set1("u_refPx", o.refPx);
    set1("u_refFactor", o.refFactor);
    set1("u_dispersion", o.dispersion);
    set1("u_fresnelRange", o.fresnelRange);
    set1("u_fresnelHardness", o.fresnelHardness);
    set1("u_fresnelFactor", o.fresnelFactor);
    set1("u_glareRange", o.glareRange);
    set1("u_glareHardness", o.glareHardness);
    set1("u_glareConvergence", o.glareConvergence);
    set1("u_glareOpposite", o.glareOpposite);
    set1("u_glareFactor", o.glareFactor);
    set1("u_glareAngle", (o.glareAngleDeg * Math.PI) / 180);
    set1("u_fadeStart", o.fadeStart);
    set1("u_blurEdge", o.blurEdge ? 1 : 0);
    set1("u_saturate", RIM_MATERIAL.saturate);
    gl.uniform4fv(uniform(gl, m, "u_tint"), RIM_MATERIAL.tint);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    // 按胶囊位置贴回各自的 canvas（胶囊按压时会 scale(1.02)，取样区用变换后的矩形，
    // 画布本身随胶囊一起缩放，两者抵消）
    const dpr = window.devicePixelRatio || 1;
    const k = W / bw;
    for (const { capsule, canvas, rect } of shapes) {
      const cw = Math.max(1, Math.round(capsule.offsetWidth * dpr));
      const ch = Math.max(1, Math.round(capsule.offsetHeight * dpr));
      if (canvas.width !== cw || canvas.height !== ch) {
        canvas.width = cw;
        canvas.height = ch;
      }
      const c2d = canvas.getContext("2d");
      if (!c2d) continue;
      c2d.clearRect(0, 0, cw, ch);
      // 画出第一帧后才切到「WebGL 厚边 + 浅色中心」的 CSS，避免先闪一下没有边的浅玻璃
      capsule.dataset.rim = "on";
      c2d.drawImage(
        this.glCanvas,
        (rect.left - left) * k,
        (rect.top - top) * k,
        rect.width * k,
        rect.height * k,
        0,
        0,
        cw,
        ch,
      );
    }
  }
}
