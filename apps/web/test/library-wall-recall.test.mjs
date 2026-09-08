import assert from "node:assert/strict";
import test from "node:test";

import {
  isReentry,
  RECALL_MAX_AGE_MS,
  RECALL_MIN_OFFSET,
  readWallRecall,
  wallRecallScope,
  writeWallRecall,
} from "../lib/library-wall-recall.ts";

/** 内存版 localStorage：只实现这三个方法，与浏览器里那份接口一致。 */
function memoryStorage(initial = {}) {
  const store = { ...initial };
  return {
    store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      store[k] = String(v);
    },
    removeItem: (k) => {
      delete store[k];
    },
  };
}

const SCOPE = wallRecallScope(7);
const NOW = 1_700_000_000_000;

test("记下的位置按同一面墙读得回来", () => {
  const storage = memoryStorage();
  writeWallRecall(SCOPE, "wall:title", 320, storage, NOW);
  assert.deepEqual(readWallRecall(SCOPE, "wall:title", storage, NOW), {
    offset: 320,
    view: "wall:title",
    updatedAt: NOW,
  });
});

test("换了墙的形态就当作没记过（同一个位置指向的不是同一部作品）", () => {
  const storage = memoryStorage();
  writeWallRecall(SCOPE, "wall:title", 320, storage, NOW);
  assert.equal(readWallRecall(SCOPE, "gallery", storage, NOW), null);
  assert.equal(readWallRecall(wallRecallScope(8), "wall:title", storage, NOW), null);
});

test("太浅的位置不提示：一两屏用户自己滑更快", () => {
  const storage = memoryStorage();
  writeWallRecall(SCOPE, "wall:title", RECALL_MIN_OFFSET - 1, storage, NOW);
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW), null);
  writeWallRecall(SCOPE, "wall:title", RECALL_MIN_OFFSET, storage, NOW);
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW)?.offset, RECALL_MIN_OFFSET);
});

test("太旧的记录不再提示", () => {
  const storage = memoryStorage();
  writeWallRecall(SCOPE, "wall:title", 320, storage, NOW);
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW + RECALL_MAX_AGE_MS + 1), null);
  assert.ok(readWallRecall(SCOPE, "wall:title", storage, NOW + RECALL_MAX_AGE_MS));
});

test("同一面墙覆盖上一条，超限时淘汰最久未更新的墙", () => {
  const storage = memoryStorage();
  writeWallRecall(SCOPE, "wall:title", 320, storage, NOW);
  writeWallRecall(SCOPE, "wall:title", 640, storage, NOW + 1000);
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW + 1000)?.offset, 640);

  // 最早写入的那一面墙（0 号）应当在写满 20 面之后被挤掉
  for (let i = 0; i < 21; i += 1) {
    writeWallRecall(wallRecallScope(100 + i), "wall:title", 100 + i, storage, NOW + 2000 + i);
  }
  assert.equal(readWallRecall(wallRecallScope(100), "wall:title", storage, NOW + 3000), null);
  assert.equal(
    readWallRecall(wallRecallScope(120), "wall:title", storage, NOW + 3000)?.offset,
    120,
  );
});

test("存储内容损坏时当作没记过，不抛异常", () => {
  const storage = memoryStorage({ "movieclaw.wall-recall": "{不是 JSON" });
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW), null);
  writeWallRecall(SCOPE, "wall:title", 320, storage, NOW);
  assert.equal(readWallRecall(SCOPE, "wall:title", storage, NOW)?.offset, 320);
});

test("读不到 storage（隐私模式）时静默降级", () => {
  const failing = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {},
  };
  assert.equal(readWallRecall(SCOPE, "wall:title", failing, NOW), null);
  assert.doesNotThrow(() => writeWallRecall(SCOPE, "wall:title", 320, failing, NOW));
});

/* —— 久别回归（iOS PWA 恢复应用不重新加载页面）—— */

test("回到前台的时刻晚于上次在这面墙滚动的时刻 = 重新进入", () => {
  assert.equal(isReentry(NOW, NOW - 1000), true);
});

test("这面墙的滚动比回归更晚（人回来后已经在这面墙上滑过）= 不再当重新进入", () => {
  assert.equal(isReentry(NOW, NOW + 1000), false);
});

test("从没挂过久后台就不是重新进入", () => {
  assert.equal(isReentry(0, undefined), false);
  assert.equal(isReentry(0, NOW - 1000), false);
});

test("挂过久后台、这面墙却没有记录：也算重新进入（没有记录=没在这面墙滑过）", () => {
  assert.equal(isReentry(NOW, undefined), true);
});
