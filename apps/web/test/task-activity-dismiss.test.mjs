import assert from "node:assert/strict";
import test from "node:test";

import { jobIsHistorical, jobNeedsAttention } from "../lib/job-attention.ts";

function job({ status = "failed", dismissedAt = null } = {}) {
  return { id: "job_1", status, dismissed_at: dismissedAt, dismissed_by: null };
}

// issue #221：失败任务此前没有任何出口，永远赖在「需要处理」里，
// 侧栏红角标于是永不熄灭。忽略补上的正是这个出口。

test("失败任务默认要用户处理", () => {
  assert.equal(jobNeedsAttention(job()), true);
  assert.equal(jobIsHistorical(job()), false);
});

test("忽略后失败任务从「需要处理」移到「已结束」", () => {
  const dismissed = job({ dismissedAt: "2026-08-27T10:00:00Z" });
  assert.equal(jobNeedsAttention(dismissed), false);
  assert.equal(jobIsHistorical(dismissed), true);
});

test("忽略不改写状态：它仍然是一条失败记录", () => {
  const dismissed = job({ dismissedAt: "2026-08-27T10:00:00Z" });
  assert.equal(dismissed.status, "failed");
});

test("blocked 任务同样受忽略影响，但它的正常出口是取消", () => {
  assert.equal(jobNeedsAttention(job({ status: "blocked" })), true);
  // blocked 仍占着去重键与资源锁，忽略接口不对它开放（见 services/jobs.dismiss_job）；
  // 真被忽略了也不该继续报警，判定本身保持一致。
  assert.equal(
    jobNeedsAttention(job({ status: "blocked", dismissedAt: "2026-08-27T10:00:00Z" })),
    false,
  );
});

test("成功与取消不受忽略影响，始终算已结束", () => {
  for (const status of ["succeeded", "cancelled"]) {
    assert.equal(jobNeedsAttention(job({ status })), false);
    assert.equal(jobIsHistorical(job({ status })), true);
  }
});

test("进行中的任务既不是待处理也不是历史", () => {
  for (const status of ["queued", "running", "retry_wait", "waiting", "cancelling"]) {
    assert.equal(jobNeedsAttention(job({ status })), false);
    assert.equal(jobIsHistorical(job({ status })), false);
  }
});
