import assert from "node:assert/strict";
import test from "node:test";

import {
  dailyCron,
  dailyTimeOf,
  describeSchedule,
  suggestReconcileInterval,
} from "../lib/scheduled-tasks.ts";

test("周期文案：间隔按小时 / 分钟说人话，每天固定时刻认得出来", () => {
  assert.equal(describeSchedule({ trigger_type: "interval", interval_seconds: 21600, cron_expr: null }), "每 6 小时");
  assert.equal(describeSchedule({ trigger_type: "interval", interval_seconds: 900, cron_expr: null }), "每 15 分钟");
  assert.equal(describeSchedule({ trigger_type: "interval", interval_seconds: 0, cron_expr: null }), "间隔未设置");
  assert.equal(describeSchedule({ trigger_type: "cron", interval_seconds: null, cron_expr: "30 3 * * *" }), "每天 03:30");
  assert.equal(describeSchedule({ trigger_type: "cron", interval_seconds: null, cron_expr: "0 */2 * * 1" }), "cron：0 */2 * * 1");
});

test("每天固定时刻的 cron 形状：只认「分 时 * * *」", () => {
  assert.deepEqual(dailyTimeOf("5 4 * * *"), { hour: 4, minute: 5 });
  assert.equal(dailyTimeOf("5 4 1 * *"), null);
  assert.equal(dailyTimeOf("60 4 * * *"), null);
  assert.equal(dailyTimeOf(null), null);
  assert.equal(dailyCron(2, 0), "0 2 * * *");
});

test("对账建议：只在有网络挂载库且周期比一小时长时提", () => {
  const six = { trigger_type: "interval", interval_seconds: 21600, cron_expr: null };
  const one = { trigger_type: "interval", interval_seconds: 3600, cron_expr: null };
  const daily = { trigger_type: "cron", interval_seconds: null, cron_expr: "0 3 * * *" };
  assert.equal(suggestReconcileInterval(six, true), true);
  assert.equal(suggestReconcileInterval(one, true), false);
  assert.equal(suggestReconcileInterval(daily, true), true);
  assert.equal(suggestReconcileInterval(six, false), false);
});
