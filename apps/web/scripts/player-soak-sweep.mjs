#!/usr/bin/env node
/**
 * 拖动压测的大范围扫参数（配套 test/player-soak.test.mjs）。
 *
 * 仓库里的那个测试文件跑的是一个**固定**矩阵：形态 × 指针频率 × 链路 × 三个
 * 种子，几十场，为的是每次改动都跑得起、跑得快。这个脚本是它的另一面——把
 * 参数空间撑到几百上千场去**找**问题，改播放器的拖动/取流那几条路之后手动跑
 * 一次，比把矩阵铺到 CI 里更划算（跑一轮几分钟，而 CI 每个 PR 都要等）。
 *
 * 它和测试文件共用同一个 `soak()`，所以两边看到的是同一套接线、同一套不变式。
 *
 * 用法::
 *
 *     node scripts/player-soak-sweep.mjs                # 默认 480 场
 *     node scripts/player-soak-sweep.mjs --seeds 20     # 更多种子
 *     node scripts/player-soak-sweep.mjs --gestures 100 # 每场更多手势
 *
 * 打破不变式时会按「同一类」归并后打印，附上可复现的完整配置——种子固定，
 * 把那行配置贴进测试文件就能单独复跑。
 */

import { soak } from "./player-soak-harness.mjs";

const MBPS = 1_000_000;

/** 参数空间。每一档都对应一类真实会踩到的情况，不是随便撒点 */
const MODES = ["direct", "transcode"];
/** 30 = 鼠标，60/120 = 手机触屏，240 = 数位笔 */
const POINTER_HZ = [30, 60, 120, 240];
const LINKS = [
  { name: "常速 25/8", linkBps: 25 * MBPS, bitrateBps: 8 * MBPS },
  { name: "慢线路 5/8", linkBps: 5 * MBPS, bitrateBps: 8 * MBPS },
  { name: "千兆 1000/4", linkBps: 1000 * MBPS, bitrateBps: 4 * MBPS },
  { name: "拖不动 3/20", linkBps: 3 * MBPS, bitrateBps: 20 * MBPS },
  { name: "原盘 80/80", linkBps: 80 * MBPS, bitrateBps: 80 * MBPS },
];
/** 两小时电影 / 十分钟短剧 / 半分钟片段 / 五秒（踩片尾余量） */
const DURATIONS = [7200, 600, 30, 5];

function parseArgs(argv) {
  const out = { seeds: 3, gestures: 40 };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, "");
    const value = Number(argv[i + 1]);
    if (key in out && Number.isFinite(value) && value > 0) out[key] = Math.floor(value);
  }
  return out;
}

function main() {
  const { seeds, gestures } = parseArgs(process.argv.slice(2));
  /** 同一类问题只留第一个样例 + 出现次数，否则一个 bug 会刷几百行 */
  const groups = new Map();
  let runs = 0;
  let broken = 0;
  const worst = { hold: 0, settle: 0, readout: 0, speed: 0, bitrate: 0 };

  for (const mode of MODES) {
    for (const pointerHz of POINTER_HZ) {
      for (const link of LINKS) {
        for (const durationS of DURATIONS) {
          for (let seed = 1; seed <= seeds; seed += 1) {
            const config = {
              mode,
              pointerHz,
              linkBps: link.linkBps,
              bitrateBps: link.bitrateBps,
              durationS,
              gestures,
              seed,
            };
            runs += 1;
            const { failures, stats } = soak(config);
            worst.hold = Math.max(worst.hold, stats.worstHoldGap);
            worst.settle = Math.max(worst.settle, stats.worstSettleGap);
            worst.readout = Math.max(worst.readout, stats.worstReadoutGap);
            worst.speed = Math.max(worst.speed, stats.worstSpeedRatio);
            worst.bitrate = Math.max(worst.bitrate, stats.worstBitrateRatio);
            const lines = [...failures];
            if (!stats.idleAtEnd) lines.push(`在途状态没清干净 ${JSON.stringify(stats)}`);
            if (stats.pendingTimers > 0) lines.push(`还挂着 ${stats.pendingTimers} 个计时器`);
            if (lines.length) broken += 1;
            for (const line of lines) {
              // 把时刻与数字抹掉再归并：同一个 bug 每场的数字都不一样
              const key = line.replace(/@\d+ms/, "").replace(/[\d.]+/g, "N");
              if (!groups.has(key)) groups.set(key, { count: 0, sample: line, config });
              groups.get(key).count += 1;
            }
          }
        }
      }
    }
  }

  console.log(`跑了 ${runs} 场（每场 ${gestures} 个手势），其中 ${broken} 场打破不变式`);
  console.log(
    "最差值：" +
      `手指停住后画面最近差 ${worst.hold}ms / 手势收尾差 ${worst.settle}ms / ` +
      `两个读数差 ${worst.readout}ms / 速率 ${worst.speed.toFixed(2)}× 真实链路 / ` +
      `码率 ${worst.bitrate.toFixed(2)}× 真值`,
  );
  for (const [, group] of groups) {
    console.log(`\n[${group.count} 场] ${group.sample}`);
    console.log(`   复现配置: ${JSON.stringify(group.config)}`);
  }
  return groups.size === 0 ? 0 : 1;
}

process.exitCode = main();
