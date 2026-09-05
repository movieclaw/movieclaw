import assert from "node:assert/strict";
import test from "node:test";

import {
  downloadGroupNeedsAttention,
  downloadTaskNeedsAttention,
  groupDownloadTasks,
} from "../lib/download-attention.ts";

function task(overrides = {}) {
  return {
    id: "1:abc",
    info_hash: "ABC",
    name: "Some.Torrent",
    media_item_id: null,
    media_title: null,
    media_kind: null,
    poster_url: null,
    state: "downloading",
    source: "subscription",
    can_replace: false,
    subscriptions: [],
    ...overrides,
  };
}

// 真实教训：下载器里几十个陈年 error 种子把「需要处理」塞满，用户点开只看到
// "检查下载器"，而下载器本身是正常的。判定必须区分"谁的任务"和"错在哪"。

test("订阅任务在下载器里报错要用户处理", () => {
  assert.equal(downloadTaskNeedsAttention(task({ state: "error" }), null), true);
  assert.equal(downloadTaskNeedsAttention(task({ state: "missing" }), null), true);
  assert.equal(downloadTaskNeedsAttention(task({ can_replace: true }), null), true);
});

test("手动下载的任务同样受 MovieClaw 照看", () => {
  assert.equal(downloadTaskNeedsAttention(task({ source: "manual", state: "error" }), null), true);
});

test("外部任务只观察不报警：它没有工单可救、没有入库可推", () => {
  assert.equal(downloadTaskNeedsAttention(task({ source: "external", state: "error" }), null), false);
  assert.equal(
    downloadTaskNeedsAttention(task({ source: "external", state: "error", can_replace: true }), null),
    false,
  );
});

test("正常下载中的任务不算待办", () => {
  assert.equal(downloadTaskNeedsAttention(task(), null), false);
  assert.equal(downloadTaskNeedsAttention(task({ state: "stalled" }), null), false);
  assert.equal(downloadTaskNeedsAttention(task({ state: "unknown" }), null), false);
});

test("关联入库 Job 失败时算待办，被忽略后不再算", () => {
  const failed = { id: "job_1", status: "failed", dismissed_at: null };
  assert.equal(downloadTaskNeedsAttention(task(), failed), true);
  assert.equal(
    downloadTaskNeedsAttention(task(), { ...failed, dismissed_at: "2026-09-05T00:00:00Z" }),
    false,
  );
});

test("分组按 infohash 反查入库 Job，任一任务待办则整组待办", () => {
  const groups = groupDownloadTasks([
    task({ id: "1:a", info_hash: "AAA", media_item_id: 7, media_title: "剧" }),
    task({ id: "1:b", info_hash: "BBB", media_item_id: 7, media_title: "剧", state: "error" }),
    task({ id: "1:c", info_hash: "CCC", source: "external", state: "error" }),
  ]);
  assert.equal(groups.length, 2);
  const byKey = new Map(groups.map((group) => [group.key, group]));
  assert.equal(downloadGroupNeedsAttention(byKey.get("media:7"), new Map()), true);
  assert.equal(downloadGroupNeedsAttention(byKey.get("task:1:c"), new Map()), false);
  // 入库 Job 用小写 hash 建索引，任务侧的大写 hash 也要能命中
  const jobs = new Map([["aaa", { id: "job_2", status: "blocked", dismissed_at: null }]]);
  assert.equal(
    downloadGroupNeedsAttention(
      groupDownloadTasks([task({ info_hash: "AAA", media_item_id: 8 })])[0],
      jobs,
    ),
    true,
  );
});
