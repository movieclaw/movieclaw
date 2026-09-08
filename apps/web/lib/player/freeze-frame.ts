/**
 * 换流/远跳时把上一帧冻在画面上，替掉那一下黑屏（docs/design/player-feel.md §2.G2）。
 *
 * **黑屏不是错觉，是 MSE 的必然**：hls.js 处理一次越出缓冲的跳转时会把
 * SourceBuffer 里当前那段一起清掉，从新落点重新装载；换会话更彻底——引擎
 * 销毁会把 `src` 摘干净。这两种情况下 `<video>` 手里一帧都没有，浏览器只能
 * 画黑（有 poster 的话闪一下海报，同样是「画面没了」）。局域网里这段空窗
 * 只有一两百毫秒看不太出来，外网上是实打实的两三秒。
 *
 * 做法照各家播放器的通例：跳转前把当前帧 `drawImage` 进一张 canvas，盖在
 * `<video>` 上，等新位置真的出画了再撤。用户看到的是「画面停住 → 转圈 →
 * 新画面」，而不是「黑屏 → 转圈 → 新画面」。少一次黑屏，观感差别很大。
 *
 * 撤的判据必须宽（`canReleaseFreeze` 由 seeked / playing / canplay /
 * timeupdate 四个事件轮流来问）：撤不掉的代价是画面永远冻着而声音在走，
 * 比黑屏糟得多，所以宁可早撤一帧也不能漏。
 */

/**
 * 冻结帧的最大宽度（像素）。
 *
 * 4K 源逐帧原样画进 canvas 是 8MB 的一张位图，手机上光这次分配就够卡一下——
 * 而这张图存在的全部意义只是「别让画面黑掉」，960 宽在任何屏幕上都足够骗过
 * 那两三秒。等比缩放由 `object-contain` 在 CSS 层负责，这里不管显示尺寸。
 */
export const FREEZE_FRAME_MAX_WIDTH = 960;

/**
 * 把 `<video>` 的当前帧画进 canvas。成功返回 true，画不出来返回 false
 * （调用方据此决定要不要盖上去——没抓到就老老实实黑一下，别盖一张空白）。
 *
 * 抓不到帧的正常情况：还没出过画（readyState < HAVE_CURRENT_DATA）、
 * 上一次换流已经把流摘掉了、浏览器不给 2d 上下文。都不是错误，静默返回。
 */
export function captureFrame(video: HTMLVideoElement, canvas: HTMLCanvasElement): boolean {
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!width || !height || video.readyState < 2) return false;
  const scale = Math.min(1, FREEZE_FRAME_MAX_WIDTH / width);
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d");
  if (!context) return false;
  try {
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
  } catch {
    // 跨源取流且没带 CORS 头时画不出来。这条路上黑屏照旧，但绝不能因此报错。
    return false;
  }
  return true;
}

/**
 * 把一格 trickplay 缩略图画进 canvas，**用落点的画面替掉上一帧**。
 *
 * 远跳（拖出缓冲、换会话）与近跳的体感差别，很大一块其实不是「等得久」而是
 * 「等的时候屏幕还停在原地」：用户已经把进度条拖到 1:20:00，画面却还是
 * 0:40:00 那一帧，于是这一拖看着像没生效，直到几秒后画面才忽然换过去。
 * 换成落点的缩略图，视觉上这一跳**当场就落地了**，之后等的只是「动起来」
 * ——各家（YouTube 的 storyboard、Netflix）都是这么处理远跳的。
 *
 * 图源就是进度条气泡在用的那张雪碧图，多半已经在浏览器缓存里；没有 trickplay
 * 索引（还没生成）时调用方退回抓当前帧，画面照旧不黑，只是不「落地」。
 *
 * `offsetX/offsetY` 沿用 `trickplay.tileAt` 的 CSS background-position 口径
 * （负值），所以取源矩形时要取反。
 */
export function drawTile(
  canvas: HTMLCanvasElement,
  image: HTMLImageElement,
  tile: { width: number; height: number; offsetX: number; offsetY: number },
): boolean {
  if (tile.width <= 0 || tile.height <= 0) return false;
  if (!image.naturalWidth || !image.naturalHeight) return false;
  canvas.width = tile.width;
  canvas.height = tile.height;
  const context = canvas.getContext("2d");
  if (!context) return false;
  try {
    context.drawImage(
      image,
      -tile.offsetX,
      -tile.offsetY,
      tile.width,
      tile.height,
      0,
      0,
      tile.width,
      tile.height,
    );
  } catch {
    return false;
  }
  return true;
}

/**
 * 冻结帧能不能撤了：新位置的那一帧已经解出来，且不在跳转途中。
 *
 * `readyState >= HAVE_CURRENT_DATA(2)` 是「当前播放位置有一帧可画」的规范
 * 定义，正是我们等的那件事。`seeking` 要一并排除——跳转途中 readyState 可能
 * 还留着旧值，这时撤会露出一瞬间的黑，等于白冻。
 */
export function canReleaseFreeze(input: { seeking: boolean; readyState: number }): boolean {
  return !input.seeking && input.readyState >= 2;
}

/**
 * 冻结帧的兜底时限（毫秒）。
 *
 * 四个事件全都没来（流起不来、自动播放被拦、解码器彻底卡死）时，到点强撤。
 * 撤完露出的是黑屏加转圈或错误页——那正是这些情况下**应该**看到的东西，
 * 而一直冻着会让用户以为播放器假死。
 */
export const FREEZE_FRAME_MAX_MS = 15_000;
