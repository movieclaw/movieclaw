/**
 * 「真实用户猛拖进度条」的压力仿真：**固定矩阵**（docs/design/player-feel.md §16）。
 *
 * 仿真装置本体在 `scripts/player-soak-harness.mjs`——那里把真实模块
 * （timeline / scrub-follow / seek-batch / bandwidth / qoe）按 video-player.tsx
 * 与 player-controls.tsx 的接线串起来，配一个行为贴规范的假 `<video>` 和一个带
 * 种子的手势生成器，逐帧查不变式。
 *
 * 这个文件只管**跑哪些场**：
 *
 * - 形态矩阵（形态 × 指针频率 × 链路 × 种子），每次改动都跑，十几秒；
 * - 几条回归用例，每条都带「改动前」的开关，先断言旧接线**确实**会被抓出来
 *   ——「灌不脏说明这条回归测试没测到东西」，同一条纪律对整套压测也成立；
 * - 想撑到几百上千场去找新问题，跑 `node scripts/player-soak-sweep.mjs`。
 *
 * **查的是三件事**（正是反馈里那三样）：
 *
 * 1. **进度**：文字读数与进度条自绘必须同源；手指停住后画面要到过落点；
 *    手势收尾后画面要追上读数；在途状态不许泄漏。
 * 2. **播放质量**：不许因为拖动而把 QoE 的卡顿时长/跳转次数灌脏。
 * 3. **速率**：取流速度读数不许超出真实链路速率，也不许长时间空白；码率
 *    读数要贴着真实码率。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { soak } from "../scripts/player-soak-harness.mjs";

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

const MBPS = 1_000_000;

/**
 * 形态矩阵：两种取流形态 × 四种指针频率 × 三种链路/片长组合 × 三个种子。
 *
 * 指针频率必须分档跑：30Hz 的鼠标、120Hz 的手机触屏、240Hz 的数位笔，
 * 合帧那条路在三种频率下的行为不一样（player-feel.md §2.C4）。链路与片长
 * 同理——五秒的短片会踩到片尾余量，千兆链路会踩到速率读数的下限。
 */
const LINKS = [
  { name: "常速", linkBps: 25 * MBPS, bitrateBps: 8 * MBPS, durationS: 7200 },
  { name: "千兆", linkBps: 1000 * MBPS, bitrateBps: 4 * MBPS, durationS: 600 },
  { name: "短片", linkBps: 25 * MBPS, bitrateBps: 8 * MBPS, durationS: 5 },
];
const MATRIX = [];
for (const mode of ["direct", "transcode"]) {
  for (const pointerHz of [30, 60, 120, 240]) {
    for (const link of LINKS) {
      for (const seed of [1, 7, 42]) MATRIX.push({ mode, pointerHz, link, seed });
    }
  }
}

for (const { mode, pointerHz, link, seed } of MATRIX) {
  test(`压测：${mode} / 指针 ${pointerHz}Hz / ${link.name} / 种子 ${seed}`, () => {
    const { failures, stats } = soak({
      seed,
      mode,
      pointerHz,
      gestures: 40,
      linkBps: link.linkBps,
      bitrateBps: link.bitrateBps,
      durationS: link.durationS,
    });
    assert.deepEqual(failures, [], `不变式被打破：\n${failures.join("\n")}\n统计：${JSON.stringify(stats)}`);
    // QoE 的「拖动次数」必须贴着用户真的跳了几次，不能被跟随写的 currentTime 灌脏
    assert.ok(
      stats.seekCount <= stats.userSeeks * 2 + 5,
      `seek_count=${stats.seekCount} 被灌脏（用户实际跳了 ${stats.userSeeks} 次）`,
    );
    // 手势全部结束后不许留在途状态
    assert.ok(stats.idleAtEnd, `跑完仍有在途状态：${JSON.stringify(stats)}`);
  });
}

const SLOW = {
  seed: 99,
  mode: "transcode",
  pointerHz: 120,
  linkBps: 5 * MBPS,
  bitrateBps: 8 * MBPS,
  durationS: 7200,
};

test("压测：慢线路上一动不动地播，卡顿要如实记下来", () => {
  const { failures, stats } = soak({ ...SLOW, gestures: 0, minMs: 60_000 });
  assert.deepEqual(failures, [], `不变式被打破：\n${failures.join("\n")}`);
  assert.ok(stats.rebufferCount > 0, "链路吃不下码率却一次卡顿都没记");
  assert.equal(stats.seekCount, 0, "没人拖过，却记了跳转");
});

test("回归：慢线路上猛拖，不许把卡顿指标灌脏（换会话的等待不是卡顿）", () => {
  // qoe.ts 开头就写着这条：「卡顿必须排除 seek 引起的 waiting，不排除的话
  // 用户拖一下进度条就被记成一次卡顿，数据全废」。改动前它在**换会话**那条
  // 路上不成立——新流从自己时间轴的 0 秒起播，hls.js 一次都不 seek，元素的
  // seeking 永远不来，于是那段等待没有闸挡着，一路记成卡顿时长。
  //
  // 灌脏的是**时长**而不是次数：换会话一次接一次，几段等待会并成一段长的，
  // 次数看不出异常，`rebuffer_ms` 却把用户自己要求的跳转全算成了卡顿。
  const baseline = soak({ ...SLOW, gestures: 0, minMs: 60_000 }).stats;
  const legacy = soak({ ...SLOW, gestures: 30, legacyQoeGate: true }).stats;
  const fixed = soak({ ...SLOW, gestures: 30 }).stats;
  const share = (x) => x.rebufferMs / x.totalMs;

  assert.ok(
    share(legacy) > share(baseline) * 1.5,
    `改动前应当把换会话的等待记成卡顿（实测占比 ${(share(legacy) * 100).toFixed(0)}% ` +
      `vs 基线 ${(share(baseline) * 100).toFixed(0)}%）——灌不脏说明这条回归测试没测到东西`,
  );
  assert.ok(
    share(fixed) <= share(baseline),
    `猛拖之后卡顿占比 ${(share(fixed) * 100).toFixed(0)}% 高于基线 ` +
      `${(share(baseline) * 100).toFixed(0)}%，指标被拖动灌脏了`,
  );
});

test("回归：换会话那条路上的跳转必须进得了质量数据", () => {
  const legacy = soak({ ...SLOW, gestures: 30, legacyQoeGate: true }).stats;
  const fixed = soak({ ...SLOW, gestures: 30 }).stats;
  assert.ok(legacy.restarts > 5, "这一场应当有不少次换会话，否则测不到东西");
  // 改动前：restart 路上元素不发 seeking，这些跳转在数据里根本不存在
  assert.ok(
    legacy.seekCount < legacy.userSeeks / 2,
    `改动前应当漏记大部分跳转（实测记了 ${legacy.seekCount}/${legacy.userSeeks}）`,
  );
  assert.equal(
    fixed.seekCount,
    fixed.userSeeks,
    `跳转次数应当与用户实际跳的次数一致（${fixed.seekCount} vs ${fixed.userSeeks}）`,
  );
  // 「跳转耗时」也要量到——它是用户报「拖拽不丝滑」时唯一能量化的数字
  assert.ok(
    fixed.lastSeekMs !== null,
    "换会话那条路跑完，跳转耗时仍然是 null——最贵的一跳没被量到",
  );
});

test("压测：千兆局域网（快到极致）速率那一格不能长时间空白", () => {
  const { failures, stats } = soak({
    seed: 5,
    mode: "transcode",
    pointerHz: 120,
    gestures: 20,
    linkBps: 1000 * MBPS,
    bitrateBps: 4 * MBPS,
    durationS: 7200,
  });
  assert.deepEqual(failures, [], `不变式被打破：\n${failures.join("\n")}\n统计：${JSON.stringify(stats)}`);
  assert.ok(
    stats.blankSpeedRatio < 0.5,
    `速率读数有 ${(stats.blankSpeedRatio * 100).toFixed(0)}% 的时间是空白`,
  );
});

test("热身之后再猛拖，速率那一格不许消失——换会话的空档要保留上一个读数", () => {
  // 起播头十几秒还没有一个分片到货时那一格本来就该空着（宁可不显示也不显示
  // 错的）。但一旦量到过，后面无论用户怎么拖、换多少次会话，组件层都必须把
  // 上一个读数留着——转圈的时候恰恰是用户最想看它的时刻。
  const cold = soak({ ...SLOW, gestures: 30 }).stats;
  const warm = soak({ ...SLOW, gestures: 30, warmupMs: 25_000 }).stats;
  assert.ok(cold.blankSpeedRatio > 0.9, "起播就猛拖时那一格应当还没有读数可显示");
  assert.ok(
    warm.blankSpeedRatio < 0.5,
    `热身之后那一格仍有 ${(warm.blankSpeedRatio * 100).toFixed(0)}% 的时间是空白——` +
      "换会话把读数清掉了",
  );
});

// ---------------------------------------------------------------------------
// 这套压测有没有牙：把前几轮修掉的 bug 放回去，它必须当场报出来
//
// 「灌不脏说明这条回归测试没测到东西」——同一条纪律对整套压测也成立。不验这
// 一步的话，一套全绿的压测和一套什么都没查的压测长得一模一样。
// ---------------------------------------------------------------------------

const FAST = {
  seed: 3,
  mode: "direct",
  pointerHz: 120,
  gestures: 40,
  linkBps: 25 * MBPS,
  bitrateBps: 8 * MBPS,
  durationS: 7200,
};

test("有牙验证：把拖动跟随退回前沿节流，压测必须报「画面与读数对不上」", () => {
  const before = soak({ ...FAST, legacyScrubThrottle: true });
  assert.ok(
    before.failures.some((line) => line.includes("手指停住后画面没到过指下位置")),
    `前沿节流应当被抓出来，实际报了：\n${before.failures.join("\n") || "（什么都没报）"}`,
  );
  assert.ok(
    before.stats.worstHoldGap > 60_000,
    `前沿节流下画面与指下位置最差差 ${(before.stats.worstHoldGap / 1000).toFixed(0)}s，量级不对`,
  );
  // 改动后同一场必须干净
  const after = soak({ ...FAST });
  assert.deepEqual(after.failures, []);
  assert.ok(
    after.stats.worstHoldGap <= 1_500,
    `改动后手指停住时仍差 ${after.stats.worstHoldGap}ms`,
  );
});

test("有牙验证：把取流速度退回「取最后一段缓冲」，压测必须报速率虚高", () => {
  const before = soak({ ...FAST, legacyProgressEstimator: true });
  assert.ok(
    before.failures.some((line) => line.includes("取流速度虚高")),
    `旧估算器应当被抓出来，实际报了：\n${before.failures.join("\n") || "（什么都没报）"}`,
  );
  assert.ok(
    before.stats.worstSpeedRatio > 10,
    `旧估算器下速率最高只虚高 ${before.stats.worstSpeedRatio.toFixed(1)}×，量级不对`,
  );
  const after = soak({ ...FAST });
  assert.deepEqual(after.failures, []);
  assert.ok(
    after.stats.worstSpeedRatio < 1.2,
    `改动后速率仍虚高 ${after.stats.worstSpeedRatio.toFixed(2)}×`,
  );
});

// 大范围探索用（scripts/perf 与临时扫参数脚本会 import 它）


test("有牙验证：跟随不夹片尾余量时，短片上拖到最右端画面会与落点差一秒", () => {
  // 松手提交走 clampSeekTarget，给片尾留一秒余量；跟随若不夹，拖到最右端时
  // 画面会被送到文件最后一刻，松手瞬间再往回跳一秒。五秒的短片上这一秒占
  // 五分之一，最好认。
  const SHORT = {
    seed: 33,
    mode: "direct",
    pointerHz: 30,
    gestures: 40,
    linkBps: 25 * MBPS,
    bitrateBps: 8 * MBPS,
    durationS: 5,
  };
  const before = soak({ ...SHORT, legacyUnclampedFollow: true });
  assert.ok(
    before.stats.worstHoldGap >= 1000,
    `不夹的话画面与落点应当差出片尾余量那一秒，实测只差 ${before.stats.worstHoldGap}ms`,
  );
  const after = soak({ ...SHORT });
  assert.deepEqual(after.failures, []);
  assert.ok(
    after.stats.worstHoldGap < 1000,
    `夹过之后仍差 ${after.stats.worstHoldGap}ms`,
  );
});
