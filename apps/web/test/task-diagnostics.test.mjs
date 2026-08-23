import assert from "node:assert/strict";
import test from "node:test";

import {
  backgroundJobDiagnosticText,
  downloadTaskDiagnosticText,
} from "../lib/task-diagnostics.ts";

test("后台任务诊断信息提供 AI 可直接查询的当前与根任务 ID", () => {
  assert.equal(
    backgroundJobDiagnosticText({
      id: "job_current",
      job_type: "library.ingest",
      status: "running",
      root_job_id: "job_root",
    }),
    [
      "MovieClaw 后台任务诊断信息",
      "job_id: job_current",
      "job_type: library.ingest",
      "status: running",
      "root_job_id: job_root",
    ].join("\n"),
  );
});

test("根后台任务不重复输出相同的根任务 ID", () => {
  assert.equal(
    backgroundJobDiagnosticText({
      id: "job_root",
      job_type: "library.scan",
      status: "queued",
      root_job_id: "job_root",
    }),
    [
      "MovieClaw 后台任务诊断信息",
      "job_id: job_root",
      "job_type: library.scan",
      "status: queued",
    ].join("\n"),
  );
});

test("下载任务诊断信息同时定位下载器、订阅和关联入库任务", () => {
  assert.equal(
    downloadTaskDiagnosticText(
      {
        id: "2:abcdef",
        info_hash: "abcdef",
        downloader_id: 2,
        source: "subscription",
        state: "completed",
        subscriptions: [{ id: 19 }, { id: 7 }, { id: 19 }],
      },
      { id: "job_ingest" },
    ),
    [
      "MovieClaw 下载任务诊断信息",
      "download_task_key: 2:abcdef",
      "info_hash: abcdef",
      "source: subscription",
      "state: completed",
      "downloader_id: 2",
      "subscription_ids: 7,19",
      "ingest_job_id: job_ingest",
    ].join("\n"),
  );
});

test("外部下载任务缺少可选关联时仍保留可用定位键", () => {
  assert.equal(
    downloadTaskDiagnosticText(
      {
        id: "missing:abcdef",
        info_hash: "abcdef",
        downloader_id: null,
        source: "external",
        state: "missing",
        subscriptions: [],
      },
      null,
    ),
    [
      "MovieClaw 下载任务诊断信息",
      "download_task_key: missing:abcdef",
      "info_hash: abcdef",
      "source: external",
      "state: missing",
    ].join("\n"),
  );
});
