import assert from "node:assert/strict";
import test from "node:test";

import {
  clientTypeLabel,
  grantBadge,
  grantSummary,
  isLive,
  relativeTime,
} from "../lib/devices-display.ts";

const MINUTE = 60_000;
const NOW = Date.parse("2026-08-29T12:00:00Z");
const ago = (ms) => new Date(NOW - ms).toISOString();

test("权限说明说人话，不出现内部权限名", () => {
  for (const type of ["worker", "cli", "manual", "什么鬼"]) {
    const { title, body } = grantSummary(type);
    const text = `${title}${body}`;
    for (const jargon of ["scope", "admin", "operate", "transcode", "token"]) {
      assert.ok(!text.includes(jargon), `「将获得」出现了内部名词 ${jargon}：${text}`);
    }
  }
});

test("转码 Worker 的说明必须点明它碰不到订阅与媒体库", () => {
  const { title, body } = grantSummary("worker");
  assert.equal(title, "将获得：仅限转码");
  assert.ok(body.includes("订阅") && body.includes("媒体库"));
});

test("命令行的说明必须点破全权的具体后果", () => {
  // v1 没有收窄手段，用户的知情就是唯一的闸（device-auth.md §4.5）：
  // 措辞退化成「完全权限」四个字就等于把闸拆了。
  const { title, body } = grantSummary("cli");
  assert.ok(title.includes("完全权限"));
  assert.ok(body.includes("删除媒体文件"), "必须写出最坏后果，而不是只说「完全权限」");
});

test("未知形态按最危险的一档解释", () => {
  // 新形态还没接上前端时，宁可把警示说重也不能说轻
  assert.deepEqual(grantSummary("未来的新客户端"), grantSummary("cli"));
  assert.equal(grantBadge("未来的新客户端"), "完全权限");
});

test("列表标注同样是实话", () => {
  assert.equal(grantBadge("worker"), "仅转码");
  assert.equal(grantBadge("cli"), "完全权限");
});

test("形态名称给人看，未知值不泄漏内部标识", () => {
  assert.equal(clientTypeLabel("worker"), "转码 Worker");
  assert.equal(clientTypeLabel("cli"), "命令行 / Agent");
  assert.equal(clientTypeLabel("manual"), "手工令牌");
  assert.equal(clientTypeLabel("weird"), "未知类型");
});

test("活跃时间按人的读法分档", () => {
  assert.equal(relativeTime(null, NOW), "从未使用");
  assert.equal(relativeTime("不是时间", NOW), "未知");
  assert.equal(relativeTime(ago(30_000), NOW), "刚刚活跃");
  assert.equal(relativeTime(ago(12 * MINUTE), NOW), "12 分钟前");
  assert.equal(relativeTime(ago(59 * MINUTE), NOW), "59 分钟前");
  assert.equal(relativeTime(ago(3 * 60 * MINUTE), NOW), "3 小时前");
  assert.equal(relativeTime(ago(3 * 24 * 60 * MINUTE), NOW), "3 天前");
});

test("在线判定用 5 分钟阈值，与令牌活跃时间的落盘粒度匹配", () => {
  assert.equal(isLive(null, NOW), false);
  assert.equal(isLive(ago(4 * MINUTE), NOW), true);
  assert.equal(isLive(ago(6 * MINUTE), NOW), false);
});
