import assert from "node:assert/strict";
import test from "node:test";

import {
  SEEK_BATCH_WINDOW_MS,
  nextSeekTarget,
  seekBatchWindowMs,
} from "../lib/player/seek-batch.ts";

test("贵的跳转才合并：转码会话 + 落点在缓冲之外", () => {
  assert.equal(seekBatchWindowMs({ hasSession: true, buffered: false }), SEEK_BATCH_WINDOW_MS);
});

test("便宜的跳转立刻执行：档 0 直出没有会话，落点已缓冲则零成本", () => {
  // 判据是「这一跳贵不贵」而不是「会不会换会话」——VOD 预生成列表同样会让
  // 服务端把 ffmpeg 杀掉重启直奔目标分片，只是不换会话而已
  assert.equal(seekBatchWindowMs({ hasSession: false, buffered: false }), 0);
  assert.equal(seekBatchWindowMs({ hasSession: true, buffered: true }), 0);
});

test("连按在累积落点上继续加，而不是每次都从当前位置起算", () => {
  const durationMs = 600_000;
  const first = nextSeekTarget({
    pendingMs: null,
    positionMs: 100_000,
    deltaMs: 10_000,
    durationMs,
  });
  assert.equal(first, 110_000);
  const second = nextSeekTarget({
    pendingMs: first,
    positionMs: 100_000,
    deltaMs: 10_000,
    durationMs,
  });
  const third = nextSeekTarget({
    pendingMs: second,
    positionMs: 100_000,
    deltaMs: 10_000,
    durationMs,
  });
  // 画面还停在 100 秒，但连按三下应该是 +30 秒而不是 +10 秒
  assert.equal(third, 130_000);
});

test("反向连按抵消回来", () => {
  const forward = nextSeekTarget({
    pendingMs: null,
    positionMs: 60_000,
    deltaMs: 10_000,
    durationMs: 600_000,
  });
  const back = nextSeekTarget({
    pendingMs: forward,
    positionMs: 60_000,
    deltaMs: -10_000,
    durationMs: 600_000,
  });
  assert.equal(back, 60_000);
});

test("累积落点照样被片尾余量与 0 夹住", () => {
  // 片尾连按快进：落点必须留出 SEEK_TAIL_GUARD_MS，否则换会话那条路会开一个
  // 转不出任何东西的会话（理由见 timeline.ts 的 clampSeekTarget）
  assert.equal(
    nextSeekTarget({ pendingMs: null, positionMs: 595_000, deltaMs: 60_000, durationMs: 600_000 }),
    599_000,
  );
  assert.equal(
    nextSeekTarget({ pendingMs: null, positionMs: 3_000, deltaMs: -10_000, durationMs: 600_000 }),
    0,
  );
  // 片长未知（服务端算不出）时不夹上界，越界交给浏览器收住
  assert.equal(
    nextSeekTarget({ pendingMs: null, positionMs: 10_000, deltaMs: 10_000, durationMs: null }),
    20_000,
  );
});
