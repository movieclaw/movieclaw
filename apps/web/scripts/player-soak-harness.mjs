/**
 * 「真实用户猛拖进度条」的压力仿真——**装置本体**（docs/design/player-feel.md §16）。
 *
 * 用例在 `test/player-soak.test.mjs`（固定矩阵，每次改动都跑），大范围扫参数在
 * `scripts/player-soak-sweep.mjs`（几百场，手动跑）。两边共用这里的 `soak()`，
 * 所以看到的是同一套接线、同一套不变式。
 *
 * 放在 `scripts/` 而不是 `test/` 是因为 `node --test` 默认会把 `test/` 下的
 * 每个 .mjs 都当测试文件加载，装置搁在那儿会被空跑一遍。
 *
 * 前八轮的单测都是「一个判据、一张输入输出表」。它们抓不到的是**组合**：
 * 拖动跟随、连按合并、会话重开、取流速度采样、QoE 计数这几条路各自都对，
 * 串在一起、再被一双手以每秒上百次的频率来回搓，才会露出问题。真机上这种
 * 问题的表现就是用户说的「进度不对、不丝滑、速率乱跳」，而它在任何单条
 * 判据的单测里都是绿的。
 *
 * 所以这里把**真实模块**（timeline / scrub-follow / seek-batch / bandwidth /
 * qoe）按 video-player.tsx 与 player-controls.tsx 的接线串起来，配一个行为贴
 * 规范的假 `<video>` 和一个带种子的手势生成器，跑几十万个虚拟毫秒，逐帧查
 * 不变式。种子固定，所以失败可复现。
 *
 * **查的是三件事**（正是反馈里那三样）：
 *
 * 1. **进度**：文字读数与进度条自绘必须同源；手势收尾后画面要追上读数；
 *    在途状态不许泄漏。
 * 2. **播放质量**：不许因为拖动而把 QoE 的卡顿/拖动次数灌脏；不许把会话
 *    反复重开成拖不动的状态。
 * 3. **速率**：取流速度读数不许超出真实链路速率一截（那是虚高），也不许
 *    长时间空白；码率读数要贴着真实码率。
 */

import {
  bandwidthBps,
  bitrateBps,
  createBandwidthWindow,
  peakBitrateBps,
  pushBandwidthSample,
  pushBitrateSample,
  readBufferedProbe,
  sampleFromProgress,
  sampleFromResourceTiming,
} from "../lib/player/bandwidth.ts";
import { initialQoe, reduceQoe, summarize } from "../lib/player/qoe.ts";
import {
  afterScrubFollow,
  initialScrubFollowState,
  planScrubFollow,
} from "../lib/player/scrub-follow.ts";
import { nextSeekTarget, seekBatchWindowMs } from "../lib/player/seek-batch.ts";
import {
  clampSeekTarget,
  isWithinRanges,
  planSeek,
  progressRatio,
  shownPositionMs,
  toFileMs,
  toSessionSeconds,
} from "../lib/player/timeline.ts";

// ---------------------------------------------------------------------------
// 虚拟时钟：setTimeout / requestAnimationFrame 都走它，1ms 一步推进
// ---------------------------------------------------------------------------

class Clock {
  constructor() {
    this.t = 0;
    this.timers = new Map();
    this.frames = new Map();
    this.seq = 0;
  }
  now() {
    return this.t;
  }
  setTimeout(fn, ms) {
    const id = ++this.seq;
    this.timers.set(id, { at: this.t + Math.max(0, ms), fn });
    return id;
  }
  clearTimeout(id) {
    this.timers.delete(id);
  }
  requestAnimationFrame(fn) {
    const id = ++this.seq;
    this.frames.set(id, fn);
    return id;
  }
  cancelAnimationFrame(id) {
    this.frames.delete(id);
  }
  /** 推进 1ms，先跑到期计时器，再（每 16ms）跑一帧 */
  step(onFrame) {
    this.t += 1;
    for (const [id, timer] of [...this.timers]) {
      if (timer.at <= this.t) {
        this.timers.delete(id);
        timer.fn();
      }
    }
    if (this.t % 16 === 0) {
      const due = [...this.frames];
      this.frames.clear();
      for (const [, fn] of due) fn();
      if (onFrame) onFrame();
    }
  }
  /** 还挂着的计时器数量（用来查在途状态泄漏） */
  pendingTimers() {
    return this.timers.size;
  }
}

/** 带种子的随机数（mulberry32）：失败可复现 */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------
// 假 <video>：贴规范的那几条
// ---------------------------------------------------------------------------

const NETWORK_IDLE = 1;
const NETWORK_LOADING = 2;

/**
 * - 写 `currentTime` 立刻改「官方播放位置」（getter 马上返回新值）并置 seeking
 * - seeking 期间不发 timeupdate；落地后发 seeked + timeupdate
 * - 播放头不会越过缓冲末端（越过就 waiting，数据来了再 playing）
 * - 只往播放头所在那段区间灌数据；前向水位到了 suspend
 * - 跳到没缓冲的位置会新开一段，旧段保留
 */
class FakeVideo {
  constructor(clock, options) {
    this.clock = clock;
    this.linkBps = options.linkBps;
    this.bitrateBps = options.bitrateBps;
    this.durationS = options.durationS;
    this.maxForwardS = options.maxForwardS ?? 60;
    this.resumeForwardS = options.resumeForwardS ?? 40;
    /** 缓冲内落地延迟 / 缓冲外落地延迟（毫秒） */
    this.nearSeekMs = options.nearSeekMs ?? 40;
    this.farSeekMs = options.farSeekMs ?? 600;
    /**
     * 卡住之后要攒够多少秒前向缓冲才恢复播放。
     *
     * 不能「有一帧就接着放」——浏览器要到 HAVE_FUTURE_DATA 才恢复，否则慢
     * 线路上会变成每帧都卡一下的退化状态（模型如果这么写，卡顿次数反而量
     * 不出来）。攒够再放才是真实的「一顿一顿地播」。
     */
    this.resumeAfterStallS = options.resumeAfterStallS ?? 2;
    this._t = 0;
    this.paused = false;
    this.seeking = false;
    this.readyState = 4;
    this.networkState = NETWORK_LOADING;
    this.ranges = [[0, 0]];
    this.loading = true;
    this.listeners = new Map();
    this.lastTimeUpdate = -1000;
    this.lastProgress = -1000;
    this._seekTimer = null;
    this.waiting = false;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  emit(type) {
    for (const fn of this.listeners.get(type) ?? []) fn();
  }
  get buffered() {
    const r = this.ranges;
    return { length: r.length, start: (i) => r[i][0], end: (i) => r[i][1] };
  }
  get seekable() {
    // 直出：整个文件都能跳。转码会话由 Player 那边替换这个 getter
    return { length: 1, start: () => 0, end: () => this.durationS };
  }
  get currentTime() {
    return this._t;
  }
  set currentTime(v) {
    this.seekTo(v);
  }
  activeIndex() {
    for (let i = 0; i < this.ranges.length; i += 1) {
      if (this._t >= this.ranges[i][0] && this._t <= this.ranges[i][1]) return i;
    }
    return -1;
  }
  fastSeek(v) {
    this.seekTo(v);
  }
  seekTo(v) {
    const target = Math.min(Math.max(0, v), this.durationS);
    // 官方播放位置立刻更新（规范：currentTime 的 setter 先设它再 seek）
    this._t = target;
    this.seeking = true;
    const inBuf = this.activeIndex() !== -1;
    if (!inBuf) {
      this.ranges.push([target, target]);
      this.ranges.sort((a, b) => a[0] - b[0]);
    }
    this.loading = true;
    this.networkState = NETWORK_LOADING;
    if (this._seekTimer !== null) this.clock.clearTimeout(this._seekTimer);
    this._seekTimer = this.clock.setTimeout(
      () => {
        this._seekTimer = null;
        this.seeking = false;
        this.emit("seeked");
        this.lastTimeUpdate = this.clock.now();
        this.emit("timeupdate");
        if (!this.paused) this.emit("playing");
      },
      inBuf ? this.nearSeekMs : this.farSeekMs,
    );
    this.emit("seeking");
  }
  /** 合并相接的区间（浏览器会这么做） */
  mergeRanges() {
    this.ranges.sort((a, b) => a[0] - b[0]);
    const out = [this.ranges[0]];
    for (let i = 1; i < this.ranges.length; i += 1) {
      const last = out[out.length - 1];
      if (this.ranges[i][0] <= last[1]) last[1] = Math.max(last[1], this.ranges[i][1]);
      else out.push(this.ranges[i]);
    }
    this.ranges = out;
  }
  tick(dtMs) {
    const now = this.clock.now();
    let i = this.activeIndex();
    if (i === -1) {
      this.ranges.push([this._t, this._t]);
      this.mergeRanges();
      i = this.activeIndex();
    }
    const forward = this.ranges[i][1] - this._t;
    if (this.loading && forward >= this.maxForwardS) {
      this.loading = false;
      this.networkState = NETWORK_IDLE;
      this.emit("suspend");
    } else if (!this.loading && forward < this.resumeForwardS) {
      this.loading = true;
      this.networkState = NETWORK_LOADING;
    }
    if (this.loading) {
      this.ranges[i][1] = Math.min(
        this.durationS,
        this.ranges[i][1] + (dtMs / 1000) * (this.linkBps / this.bitrateBps),
      );
      this.mergeRanges();
      i = this.activeIndex();
      if (now - this.lastProgress >= 350) {
        this.lastProgress = now;
        this.emit("progress");
      }
    }
    if (this.seeking || this.paused) return;
    const end = this.ranges[i][1];
    const want = this._t + dtMs / 1000;
    // **播放头要么按 1× 走、要么停住**，不能被缓冲末端夹着慢放——夹住的话
    // 慢线路会变成「以 0.6 倍速平滑播放」，一次卡顿都量不出来，而真实浏览器
    // 是一顿一顿地播。
    if (want > end && end < this.durationS) {
      if (!this.waiting) {
        this.waiting = true;
        this.emit("waiting");
      }
      return;
    }
    if (this.waiting) {
      // 攒够前向缓冲才恢复，见 resumeAfterStallS
      if (end - this._t < Math.min(this.resumeAfterStallS, this.durationS - this._t)) return;
      this.waiting = false;
      this.emit("playing");
    }
    this._t = Math.min(want, this.durationS);
    if (now - this.lastTimeUpdate >= 250) {
      this.lastTimeUpdate = now;
      this.emit("timeupdate");
    }
  }
}

// ---------------------------------------------------------------------------
// Player：video-player.tsx + player-controls.tsx 的接线
// ---------------------------------------------------------------------------

class Player {
  constructor(clock, video, options) {
    this.clock = clock;
    this.video = video;
    this.mode = options.mode; // "direct" | "transcode"
    /**
     * 用**改动前的接线**跑：QoE 的「跳了几次 / 这一跳多久」由 video 元素的
     * `seeking` 事件驱动，并用 `fromScrub` 把拖动跟随写的那些过滤掉。
     * 只给回归用例用，用来证明那条路上确实有问题。
     */
    this.legacyQoeGate = options.legacyQoeGate === true;
    /** 用**前沿**节流跑拖动跟随（第七轮修掉的那条），供回归对照 */
    this.legacyScrubThrottle = options.legacyScrubThrottle === true;
    /** 用**取最后一段缓冲**的估算器跑取流速度（第八轮修掉的那条），供回归对照 */
    this.legacyProgressEstimator = options.legacyProgressEstimator === true;
    /** 跟随不夹片尾余量（本轮修掉的那条），供回归对照 */
    this.legacyUnclampedFollow = options.legacyUnclampedFollow === true;
    this.scrubWrites = 0;
    this.trace = options.trace ? [] : null;
    this.durationMs = Math.round(video.durationS * 1000);
    this.startMs = 0;
    this.sourceBitrateBps = options.sourceBitrateBps;
    this.segmentSeconds = options.segmentSeconds ?? 6;

    // ---- 状态（对应 React state） ----
    this.positionMs = 0;
    this.dragging = null;
    this.pendingSeekMs = null;
    // ---- 在途状态 ----
    this.scrubRef = initialScrubFollowState();
    this.scrubTimer = null;
    this.pendingSeekRef = { targetMs: null, timer: null };
    this.lastProbe = null;
    this.pointer = null;
    this.pointerFrame = 0;
    // ---- 读数 ----
    this.bandwidth = createBandwidthWindow();
    this.bitrateSamples = [];
    this.qoe = initialQoe();
    /** 用户「有意」的跳转次数（提交 + 连按落地），用来核对 QoE 的 seek_count */
    this.userSeeks = 0;
    this.restarts = 0;
    /** 已经喂过几个分片（转码档；换会话后从头数） */
    this.segmentsFed = 0;
    /**
     * 组件层的速度读数：**换会话的空档里保留上一个值，不清空**
     * （video-player.tsx 的 setSpeedLabel 就是这么做的——那几秒没有引擎可问，
     * 但「上次量到的线路速度」并没有失效，而那恰恰是用户最想看它的时刻）。
     */
    this.speedLabelBps = null;

    video.addEventListener("timeupdate", () => {
      this.positionMs = toFileMs(this.video.currentTime, this.startMs);
    });
    video.addEventListener("seeking", () => {
      if (!this.legacyQoeGate) {
        // 元素的 seeking 只开「这段等待不算卡顿」的闸，不计数
        this.qoe = reduceQoe(this.qoe, { type: "seeking", at: this.clock.now() });
        return;
      }
      // 改动前：元素事件既计数又开闸，靠 fromScrub 滤掉跟随写的那些
      const fromScrub = this.scrubWrites > 0 && this.clock.now() - this.scrubRef.at < 250;
      if (!fromScrub) {
        this.qoe = reduceQoe(this.qoe, { type: "seek-requested", at: this.clock.now() });
      }
    });
    video.addEventListener("seeked", () => {
      this.qoe = reduceQoe(this.qoe, { type: "seeked", at: this.clock.now() });
    });
    video.addEventListener("waiting", () => {
      this.qoe = reduceQoe(this.qoe, { type: "waiting", at: this.clock.now() });
    });
    video.addEventListener("playing", () => {
      this.qoe = reduceQoe(this.qoe, { type: "playing", at: this.clock.now() });
    });
    video.addEventListener("progress", () => this.onProgress());
    video.addEventListener("suspend", () => {
      this.lastProbe = null;
    });
  }

  // ------------------------------------------------------- 取流速度采样
  onProgress() {
    if (this.mode !== "direct") return;
    if (this.legacyProgressEstimator) {
      // 改动前：取 buffered 的**最后一段**，且只挡住「倒退」不挡「大跳」
      const b = this.video.buffered;
      const bufferedEnd = b.length ? b.end(b.length - 1) : 0;
      const previous = this.legacyLast;
      this.legacyLast = { at: this.clock.now(), bufferedEnd };
      if (!previous) return;
      const transferMs = this.clock.now() - previous.at;
      if (transferMs > 2_000) return;
      const grown = bufferedEnd - previous.bufferedEnd;
      if (grown <= 0) return;
      this.bandwidth = pushBandwidthSample(this.bandwidth, {
        at: this.clock.now(),
        bytes: (grown * this.sourceBitrateBps) / 8,
        transferMs,
      });
      return;
    }
    const current = readBufferedProbe(this.video, this.clock.now());
    const sample = sampleFromProgress({
      previous: this.lastProbe,
      current,
      sourceBitrateBps: this.sourceBitrateBps,
    });
    this.lastProbe = current;
    if (sample) this.bandwidth = pushBandwidthSample(this.bandwidth, sample);
  }

  /** 转码档：一个分片到货（由 harness 在缓冲每涨一个分片时调用） */
  onFragLoaded() {
    const segBytes = (this.sourceBitrateBps * this.segmentSeconds) / 8;
    const transferMs = ((segBytes * 8) / this.video.linkBps) * 1000;
    this.bitrateSamples = pushBitrateSample(this.bitrateSamples, {
      bytes: segBytes,
      seconds: this.segmentSeconds,
    });
    const sample = sampleFromResourceTiming(
      {
        responseStart: this.clock.now() - transferMs,
        responseEnd: this.clock.now(),
        transferSize: segBytes + 400,
        encodedBodySize: segBytes,
      },
      this.clock.now(),
    );
    if (sample) this.bandwidth = pushBandwidthSample(this.bandwidth, sample);
  }

  // ------------------------------------------------------- 跳转
  isCheapSeek(fileMs) {
    if (this.mode === "direct") return true;
    return isWithinRanges(this.video.buffered, toSessionSeconds(fileMs, this.startMs));
  }

  cancelScrubFollow() {
    if (this.scrubTimer !== null) this.clock.clearTimeout(this.scrubTimer);
    this.scrubTimer = null;
    this.scrubRef = { ...this.scrubRef, pendingMs: null };
  }

  applyScrubFollow(rawFileMs) {
    // 与松手提交同一条夹紧规则（见实现里的注释）
    const fileMs = this.legacyUnclampedFollow
      ? rawFileMs
      : clampSeekTarget(rawFileMs, this.durationMs);
    const seconds = toSessionSeconds(fileMs, this.startMs);
    if (seconds < 0 || !this.isCheapSeek(fileMs)) {
      if (this.trace) this.trace.push(`${this.clock.now()} apply-DROP target=${fileMs} sec=${seconds.toFixed(1)} cheap=${this.isCheapSeek(fileMs)}`);
      return;
    }
    this.scrubWrites = this.clock.now() - this.scrubRef.at < 500 ? this.scrubWrites + 1 : 0;
    this.scrubRef = afterScrubFollow(this.clock.now());
    this.video.fastSeek(seconds);
  }

  scrubTo(fileMs) {
    if (this.trace) this.trace.push(`${this.clock.now()} scrubTo ${fileMs} cheap=${this.isCheapSeek(fileMs)}`);
    if (this.legacyScrubThrottle) {
      // 改动前：前沿节流，窗口里后来的移动全丢——手指一停就再没有 pointermove
      // 把最后那个落点送下去，画面钉在半路
      if (this.clock.now() - this.scrubRef.at < 100) return;
      this.applyScrubFollow(fileMs);
      return;
    }
    const plan = planScrubFollow({
      now: this.clock.now(),
      state: this.scrubRef,
      cheap: this.isCheapSeek(fileMs),
      reachable: toSessionSeconds(fileMs, this.startMs) >= 0,
    });
    if (plan.kind === "skip") return;
    if (plan.kind === "follow") {
      this.cancelScrubFollow();
      this.applyScrubFollow(fileMs);
      return;
    }
    this.scrubRef = { ...this.scrubRef, pendingMs: fileMs };
    if (this.scrubTimer !== null) this.clock.clearTimeout(this.scrubTimer);
    this.scrubTimer = this.clock.setTimeout(() => {
      this.scrubTimer = null;
      const target = this.scrubRef.pendingMs;
      if (target !== null) this.applyScrubFollow(target);
    }, plan.delayMs);
  }

  seekToFileMs(rawFileMs) {
    const fileMs = clampSeekTarget(rawFileMs, this.durationMs);
    const seekable = this.video.seekable;
    const plan = planSeek(fileMs, {
      startMs: this.startMs,
      seekableEndSeconds: seekable.length ? seekable.end(seekable.length - 1) : 0,
      hasSession: this.mode === "transcode",
    });
    // 「用户跳了一次」的唯一计数与计时起点（与实现一致）。改动前这里什么都
    // 不发，全靠 video 的 seeking 事件——而 restart 那条路上它永远不来。
    if (!this.legacyQoeGate) {
      this.qoe = reduceQoe(this.qoe, { type: "seek-requested", at: this.clock.now() });
    }
    if (plan.kind === "native") {
      const seconds = Math.max(0, plan.seconds);
      this.video.currentTime = seconds;
      this.positionMs = toFileMs(seconds, this.startMs);
      return;
    }
    // ---- 换会话 ----
    // 真实路径：video.pause() → 旧引擎销毁（src 摘掉）→ 请新会话 → 新流从
    // **自己时间轴的 0 秒**起播（所以 hls.js 一次都不 seek，元素的 seeking
    // 永远不来）→ 数据没到之前 waiting → 出画 playing。
    this.restarts += 1;
    this.positionMs = plan.startMs;
    this.startMs = plan.startMs;
    this.video.ranges = [[0, 0]];
    this.video._t = 0;
    this.video.seeking = false;
    this.video.waiting = false;
    this.video.loading = true;
    this.video.networkState = NETWORK_LOADING;
    this.lastProbe = null;
    // 引擎重建 → 引擎级的窗口从头开始（组件那层的读数保留见 speedLabel）
    this.bitrateSamples = [];
    this.bandwidth = createBandwidthWindow();
    this.segmentsFed = 0;
  }

  cancelPendingSeek() {
    if (this.pendingSeekRef.timer !== null) this.clock.clearTimeout(this.pendingSeekRef.timer);
    this.pendingSeekRef.timer = null;
    this.pendingSeekRef.targetMs = null;
    this.pendingSeekMs = null;
  }

  commitSeek(fileMs) {
    this.cancelPendingSeek();
    this.cancelScrubFollow();
    this.userSeeks += 1;
    this.seekToFileMs(fileMs);
  }

  /** 连按 ±N 秒（快捷键 / 双击） */
  seekBy(seconds) {
    const target = nextSeekTarget({
      pendingMs: this.pendingSeekRef.targetMs,
      positionMs: this.positionMs,
      deltaMs: seconds * 1000,
      durationMs: this.durationMs,
    });
    const windowMs = seekBatchWindowMs({
      hasSession: this.mode === "transcode",
      buffered: this.isCheapSeek(target),
    });
    if (windowMs <= 0) {
      this.cancelPendingSeek();
      this.userSeeks += 1;
      this.seekToFileMs(target);
      return;
    }
    this.pendingSeekRef.targetMs = target;
    this.pendingSeekMs = target;
    if (this.pendingSeekRef.timer !== null) this.clock.clearTimeout(this.pendingSeekRef.timer);
    this.pendingSeekRef.timer = this.clock.setTimeout(() => {
      this.pendingSeekRef.timer = null;
      const committed = this.pendingSeekRef.targetMs;
      this.pendingSeekRef.targetMs = null;
      this.pendingSeekMs = null;
      if (committed !== null) {
        this.userSeeks += 1;
        this.seekToFileMs(committed);
      }
    }, windowMs);
  }

  // ------------------------------------------------------- 控制条（合帧）
  get overrideMs() {
    return this.pendingSeekMs;
  }
  pointerMs() {
    if (!this.pointer || !this.durationMs || this.pointer.length <= 0) return null;
    const ratio = Math.min(1, Math.max(0, this.pointer.x / this.pointer.length));
    return Math.round(ratio * this.durationMs);
  }
  cancelPointerFrame() {
    if (this.pointerFrame) this.clock.cancelAnimationFrame(this.pointerFrame);
    this.pointerFrame = 0;
  }
  flushPointer() {
    this.pointerFrame = 0;
    const next = this.pointerMs();
    if (next === null) return;
    if (this.dragging === null) return;
    this.dragging = next;
    this.scrubTo(next);
  }
  pointerDown(ratio) {
    this.pointer = { x: ratio * 1000, length: 1000 };
    this.dragging = Math.round(ratio * this.durationMs);
  }
  pointerMove(ratio) {
    this.pointer = { x: ratio * 1000, length: 1000 };
    if (!this.pointerFrame) {
      this.pointerFrame = this.clock.requestAnimationFrame(() => this.flushPointer());
    }
  }
  pointerUp() {
    this.cancelPointerFrame();
    const last = this.pointerMs();
    if (this.dragging !== null) this.commitSeek(last ?? this.dragging);
    this.pointer = null;
    this.dragging = null;
  }
  pointerCancel() {
    this.cancelPointerFrame();
    this.pointer = null;
    this.cancelScrubFollow();
    this.dragging = null;
  }

  // ------------------------------------------------------- 读数
  textMs() {
    return shownPositionMs({
      draggingMs: this.dragging,
      overrideMs: this.overrideMs,
      livePositionMs: null,
      positionMs: this.positionMs,
    });
  }
  barMs() {
    const el = this.video;
    const live =
      el && !el.paused && !el.seeking && el.readyState >= 2
        ? toFileMs(el.currentTime, this.startMs)
        : null;
    return shownPositionMs({
      draggingMs: this.dragging,
      overrideMs: this.overrideMs,
      livePositionMs: live,
      positionMs: this.positionMs,
    });
  }
  pictureMs() {
    return toFileMs(this.video.currentTime, this.startMs);
  }
  /** 在途状态是不是都清干净了 */
  idleState() {
    return (
      this.dragging === null &&
      this.pendingSeekMs === null &&
      this.pendingSeekRef.targetMs === null &&
      this.pendingSeekRef.timer === null &&
      this.scrubRef.pendingMs === null &&
      this.scrubTimer === null &&
      this.pointerFrame === 0
    );
  }
}

// ---------------------------------------------------------------------------
// 手势生成器：模拟真实用户
// ---------------------------------------------------------------------------

/**
 * 一个手势 = 一串 `{ at, kind, ratio }` 事件。
 *
 * 六种，覆盖真机上人手真的会做的事：
 * - sweep：一口气从 a 扫到 b（最常见的「大概拖到那儿」）
 * - jitter：来回搓，反复越过同一段（「找那个镜头」）
 * - settle：扫过去然后**停住不动**再松手（最容易暴露跟随丢落点）
 * - tap：直接点一下进度条
 * - cancel：拖到一半被系统手势收走（贴着屏幕底边拖必然踩到）
 * - keys：连按 ±10 秒
 */
function makeGesture(random, { at, pointerHz }) {
  const kinds = ["sweep", "jitter", "settle", "tap", "cancel", "keys"];
  const kind = kinds[Math.floor(random() * kinds.length)];
  const a = random();
  const b = random();
  const events = [];
  const gap = 1000 / pointerHz;

  if (kind === "keys") {
    const presses = 1 + Math.floor(random() * 4);
    const step = random() < 0.5 ? -10 : 10;
    for (let i = 0; i < presses; i += 1) {
      events.push({ at: at + i * (60 + random() * 200), kind: "key", seconds: step });
    }
    return { kind, events, endAt: events[events.length - 1].at };
  }
  if (kind === "tap") {
    events.push({ at, kind: "down", ratio: a });
    events.push({ at: at + 60, kind: "up" });
    return { kind, events, endAt: at + 60 };
  }

  const durationMs = 200 + random() * 1400;
  const moves = Math.max(2, Math.round((durationMs / 1000) * pointerHz));
  events.push({ at, kind: "down", ratio: a });
  for (let i = 1; i <= moves; i += 1) {
    const p = i / moves;
    let ratio;
    if (kind === "jitter") {
      const cycles = 2 + Math.floor(random() * 7);
      ratio = a + (b - a) * (0.5 - 0.5 * Math.cos(p * Math.PI * 2 * cycles));
    } else {
      ratio = a + (b - a) * p;
    }
    events.push({ at: at + i * gap, kind: "move", ratio });
  }
  let endAt = at + moves * gap;
  if (kind === "settle") {
    // 扫完**停住不动**，手指还按着。真手指停住时浏览器就**不再发
    // pointermove**——这正是前沿节流会把最后一个落点永远丢掉的场景，
    // 手势生成器如果在停住期间继续发 move，就恰好把它遮住了。
    endAt += 1_600 + random() * 900;
  }
  events.push({ at: endAt, kind: kind === "cancel" ? "cancel" : "up" });
  return { kind, events, endAt };
}

// ---------------------------------------------------------------------------
// 跑一场
// ---------------------------------------------------------------------------

function soak({ seed, mode, pointerHz, gestures, linkBps, bitrateBps: br, durationS, legacyQoeGate, legacyScrubThrottle, legacyProgressEstimator, legacyUnclampedFollow, minMs, warmupMs, trace }) {
  const clock = new Clock();
  const video = new FakeVideo(clock, {
    linkBps,
    bitrateBps: br,
    durationS,
  });
  const player = new Player(clock, video, {
    mode,
    sourceBitrateBps: br,
    legacyQoeGate,
    legacyScrubThrottle,
    legacyProgressEstimator,
    legacyUnclampedFollow,
    trace,
  });
  if (mode === "transcode") {
    // 转码会话：seekable 只到「已转出来的时长」
    Object.defineProperty(video, "seekable", {
      get() {
        const r = this.ranges;
        return { length: 1, start: () => 0, end: () => (r.length ? r[r.length - 1][1] : 0) };
      },
    });
  }

  const random = rng(seed);
  // 手势时间表
  const schedule = [];
  let cursor = warmupMs ?? 1500; // 先播一段，让读数有东西
  for (let i = 0; i < gestures; i += 1) {
    const g = makeGesture(random, { at: cursor, pointerHz });
    schedule.push(g);
    cursor = g.endAt + 400 + random() * 1600; // 手势之间的间隔
  }
  const events = schedule.flatMap((g) => g.events).sort((x, y) => x.at - y.at);
  const totalMs = Math.max(cursor + 4000, minMs ?? 0);

  // ---- 不变式的采集 ----
  const failures = [];
  const record = (what, detail) => {
    if (failures.length < 12) failures.push(`${what} @${clock.now()}ms ${detail}`);
  };
  let lastInputAt = 0;
  let lastMoveCheap = false;
  /** 一次「停住」的核查：手指停稳后画面有没有到过落点 */
  let holdCheck = null;
  let bandwidthSeen = 0;
  let bandwidthNull = 0;
  let worstSpeedRatio = 0;
  let worstBitrateRatio = 0;
  let worstReadoutGap = 0;
  let worstSettleGap = 0;
  let worstHoldGap = 0;

  let cursorEvent = 0;
  for (let ms = 0; ms < totalMs; ms += 1) {
    // 投递到期的用户事件
    while (cursorEvent < events.length && events[cursorEvent].at <= clock.now()) {
      const e = events[cursorEvent++];
      lastInputAt = clock.now();
      holdCheck = null;
      if (e.kind === "down") player.pointerDown(e.ratio);
      else if (e.kind === "move") {
        player.pointerMove(e.ratio);
        // 跟随只在「这一跳不要钱」时发生，而这个判断只在**移动那一刻**做一次
        // （手指停住之后不再有 pointermove，也就不会重新评估——转码会话下
        // 拖到没转的段落，画面就该原地不动等松手，这是 §2.C2 定的规矩）。
        // 所以不变式要看的是停手那一刻便宜不便宜，而不是现在便宜不便宜。
        lastMoveCheap = player.isCheapSeek(Math.round(e.ratio * player.durationMs));
      }
      else if (e.kind === "up") player.pointerUp();
      else if (e.kind === "cancel") player.pointerCancel();
      else if (e.kind === "key") player.seekBy(e.seconds);
    }
    clock.step();
    video.tick(1);
    // 转码档：缓冲每涨一个分片就算一个分片到货
    if (mode === "transcode") {
      const i = video.activeIndex();
      const end = i === -1 ? 0 : video.ranges[i][1];
      while (end >= (player.segmentsFed + 1) * player.segmentSeconds) {
        player.segmentsFed += 1;
        player.onFragLoaded();
      }
    }
    // 组件层每秒问一次引擎，读不出来就保留上一个值
    if (clock.now() % 1000 === 0) {
      const live = bandwidthBps(player.bandwidth);
      if (live !== null) player.speedLabelBps = live;
    }
    player.qoe = reduceQoe(player.qoe, {
      type: "tick",
      at: clock.now(),
      playing: !video.paused && !video.seeking && !video.waiting,
    });

    if (clock.now() % 16 !== 0) continue;

    // ---- 不变式 1：文字读数与进度条自绘同源 ----
    const text = player.textMs();
    const bar = player.barMs();
    if (!Number.isFinite(text) || !Number.isFinite(bar)) record("读数变成了 NaN", `${text}/${bar}`);
    const gap = Math.abs(text - bar);
    worstReadoutGap = Math.max(worstReadoutGap, gap);
    if (gap > 1000) record("文字读数与进度条打架", `${(gap / 1000).toFixed(1)}s`);

    // ---- 不变式 2：比例永远在 [0,1] ----
    const ratio = progressRatio(bar, player.durationMs);
    if (!(ratio >= 0 && ratio <= 1)) record("进度比例越界", String(ratio));

    // ---- 不变式 3a：手指按着停住之后，画面必须**到过**指下位置 ----
    // 查「到过」而不是「此刻贴着」：落地之后画面会照常继续往前播，手指按着
    // 不动三秒，差值自然就有三秒——那不是没跟上。只在「停手那一刻这一跳
    // 不要钱」时成立（转码会话拖到没转的段落，画面就该原地不动等松手，
    // 那是 §2.C2 定的规矩）。
    const holdFor = clock.now() - lastInputAt;
    if (player.dragging !== null && holdFor >= 700 && lastMoveCheap && holdCheck === null) {
      holdCheck = {
        // 比的是**夹过的**落点：拖到最右端时 clampSeekTarget 会给片尾留一秒
        // 余量（那一秒照常播完并发 ended），差这一秒是设计如此
        target: clampSeekTarget(player.dragging, player.durationMs),
        deadline: clock.now() + 1200,
        closest: Infinity,
      };
    }
    if (holdCheck !== null) {
      holdCheck.closest = Math.min(
        holdCheck.closest,
        Math.abs(player.pictureMs() - holdCheck.target),
      );
      if (clock.now() >= holdCheck.deadline) {
        worstHoldGap = Math.max(worstHoldGap, holdCheck.closest);
        if (holdCheck.closest > 1000) {
          record("手指停住后画面没到过指下位置", `最近只差 ${(holdCheck.closest / 1000).toFixed(1)}s`);
        }
        holdCheck = null;
      }
    }

    // ---- 不变式 3b：手势收尾后画面要追上读数 ----
    const settled =
      clock.now() - lastInputAt > 1200 && !video.seeking && !video.paused && player.idleState();
    if (settled) {
      const settleGap = Math.abs(player.textMs() - player.pictureMs());
      worstSettleGap = Math.max(worstSettleGap, settleGap);
      if (settleGap > 2000) {
        record("手势收尾后画面与读数对不上", `${(settleGap / 1000).toFixed(1)}s`);
      }
    }

    // ---- 不变式 4：取流速度读数（查组件层，那才是用户看到的那一格）----
    const bps = player.speedLabelBps;
    if (bps === null) bandwidthNull += 1;
    else {
      bandwidthSeen += 1;
      worstSpeedRatio = Math.max(worstSpeedRatio, bps / linkBps);
      if (bps > linkBps * 2) {
        record("取流速度虚高", `${(bps / linkBps).toFixed(1)}× 真实链路`);
      }
    }

    // ---- 不变式 5：码率读数 ----
    const measured = bitrateBps(player.bitrateSamples);
    if (measured !== null) {
      worstBitrateRatio = Math.max(worstBitrateRatio, measured / br);
      if (measured > br * 1.5 || measured < br * 0.5) {
        record("码率读数偏离真值", `${(measured / br).toFixed(2)}×`);
      }
      const peak = peakBitrateBps(player.bitrateSamples);
      if (peak < measured) record("峰值码率小于平均码率", `${peak} < ${measured}`);
    }
  }

  const q = summarize(player.qoe);
  return {
    failures,
    trace: player.trace,
    stats: {
      seekCount: q.seek_count,
      lastSeekMs: player.qoe.lastSeekMs,
      userSeeks: player.userSeeks,
      rebufferCount: q.rebuffer_count,
      rebufferMs: q.rebuffer_ms,
      restarts: player.restarts,
      watchedMs: q.watched_ms,
      pendingTimers: clock.pendingTimers(),
      idleAtEnd: player.idleState(),
      worstReadoutGap,
      worstSettleGap,
      worstHoldGap,
      worstSpeedRatio,
      worstBitrateRatio,
      blankSpeedRatio: bandwidthNull / Math.max(1, bandwidthNull + bandwidthSeen),
      totalMs,
    },
  };
}

export { soak };
