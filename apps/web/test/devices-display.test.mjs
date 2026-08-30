import assert from "node:assert/strict";
import test from "node:test";

import {
  clientTypeLabel,
  envSnippet,
  grantBadge,
  grantSummary,
  isLive,
  manualGrantSummary,
  relativeTime,
  resolveServerAddress,
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

// ---------------------------------------------------------------------------
// 手工令牌：环境变量片段
// ---------------------------------------------------------------------------

test("手工令牌的权限说明与审批卡同权，且点破不过期与只能吊销", () => {
  const { title, body } = manualGrantSummary();
  assert.equal(title, grantSummary("manual").title, "手工令牌不能显得比批准出来的权限小");
  assert.ok(body.includes("删除媒体文件"), "全权的含义必须点破到具体后果");
  assert.ok(body.includes("不会自动过期"));
  assert.ok(body.includes("吊销"));
  // 这条路上没有「批准」这个动作，照抄审批卡的收尾会指向一个不存在的按钮
  assert.ok(!body.includes("才批准"), `手工创建的说明不该提批准：${body}`);
});

test("配过对外访问地址时直接用它，并去掉尾斜杠", () => {
  const address = resolveServerAddress("https://movieclaw.example.com/", "http://192.168.1.24:3000");
  assert.deepEqual(address, { url: "https://movieclaw.example.com", configured: true });
});

test("没配对外地址时回落当前地址，但必须标成「不是用户配的」", () => {
  const address = resolveServerAddress("", "http://192.168.1.24:3000");
  assert.deepEqual(address, { url: "http://192.168.1.24:3000", configured: false });
  // configured=false 是界面弹出「这只是猜测」那段警示的唯一依据，不能悄悄当真
});

test("只有空白的对外地址等同没配", () => {
  assert.equal(resolveServerAddress("   ", "http://127.0.0.1:3000").configured, false);
});

test("环境变量片段是可直接粘贴的两行，地址在前令牌在后", () => {
  const snippet = envSnippet("http://192.168.1.10:3000", "mclaw_abc123");
  assert.equal(snippet, "MOVIECLAW_SERVER=http://192.168.1.10:3000\nMOVIECLAW_TOKEN=mclaw_abc123");
  // KEY=value 而非 export：同一份文本要能用在 .env / --env-file / compose / source
  assert.ok(!snippet.includes("export "), "带 export 就不能直接当 .env 用");
  for (const line of snippet.split("\n")) {
    assert.match(line, /^[A-Z_]+=\S+$/, `不是干净的 KEY=value：${line}`);
  }
});
