import assert from "node:assert/strict";
import test from "node:test";

import { upgradeLadderPreview } from "../lib/upgrade-ladder.ts";

test("不洗版的规则组没有阶梯可言", () => {
  assert.equal(upgradeLadderPreview({ resolutions: ["1080p"] }), null);
});

test("缺省二元组：终点之下逐级降片源，更高分辨率整体高于终点", () => {
  const preview = upgradeLadderPreview({
    resolutions: ["2160p", "1080p"],
    upgrade_source: "blu-ray",
    cutoff_resolution: "1080p",
  });

  assert.deepEqual(preview.dimensions, ["分辨率", "片源"]);
  assert.equal(preview.target, "1080p · 蓝光");
  // 分辨率排在第一位 ⇒ 任意 2160p 都优于 1080p 蓝光，最高档是 2160p Remux
  assert.equal(preview.ceiling, "2160p · Remux");
  assert.deepEqual(preview.below, [
    "1080p · WEB-DL",
    "1080p · Rip 类",
    "1080p · 电视录制类",
  ]);
  assert.equal(preview.moreBelow, 0);
});

test("低位维度先降档，降到底才退高位一档", () => {
  const preview = upgradeLadderPreview({
    resolutions: ["1080p"],
    hdr_levels: ["DV", "HDR10"],
    upgrade_source: "remux",
    upgrade_ladder: ["resolution", "source", "hdr"],
  });

  assert.deepEqual(preview.dimensions, ["分辨率", "片源", "HDR"]);
  assert.equal(preview.target, "1080p · Remux · DV");
  assert.equal(preview.ceiling, null); // 终点已是最高档
  assert.deepEqual(preview.below, [
    "1080p · Remux · HDR10",
    "1080p · 蓝光 · DV",
    "1080p · 蓝光 · HDR10",
    "1080p · WEB-DL · DV",
    "1080p · WEB-DL · HDR10",
  ]);
  assert.equal(preview.moreBelow, 4); // WEBRip/BDRip 与 HDTV/DVD 各两档
});

test("没配偏好的维度不占位，编码按族塌缩成一位", () => {
  const preview = upgradeLadderPreview({
    resolutions: ["1080p"],
    video_codecs: ["x265", "H.265", "HEVC", "x264"],
    upgrade_source: "web-dl",
    upgrade_ladder: ["resolution", "video_codec", "platform", "source"],
  });

  assert.deepEqual(preview.dimensions, ["分辨率", "编码", "片源"]);
  assert.equal(preview.target, "1080p · H.265 · WEB-DL");
  assert.deepEqual(preview.below.slice(0, 3), [
    "1080p · H.265 · Rip 类",
    "1080p · H.265 · 电视录制类",
    "1080p · H.264 · Remux",
  ]);
});

test("平台维度用展示名，且注入的 labeller 生效", () => {
  const preview = upgradeLadderPreview(
    {
      resolutions: ["2160p"],
      platforms: ["netflix", "disney_plus"],
      upgrade_source: "web-dl",
      upgrade_ladder: ["resolution", "source", "platform"],
    },
    (id) => ({ netflix: "Netflix", disney_plus: "Disney+" })[id] ?? id,
  );

  assert.equal(preview.target, "2160p · WEB-DL · Netflix");
  assert.equal(preview.ceiling, "2160p · Remux · Netflix");
  assert.deepEqual(preview.below.slice(0, 2), [
    "2160p · WEB-DL · Disney+",
    "2160p · Rip 类 · Netflix",
  ]);
});

test("配了片源白名单时，阶梯的片源轴改用用户序", () => {
  const preview = upgradeLadderPreview({
    resolutions: ["1080p"],
    media_sources: ["web-dl", "blu-ray"], // 省流党：WEB-DL 优于蓝光
    upgrade_source: "web-dl",
  });

  assert.equal(preview.target, "1080p · WEB-DL");
  assert.equal(preview.ceiling, null); // 终点已是用户序里的最高档
  assert.deepEqual(preview.below, ["1080p · 蓝光"]); // 白名单外的档不出现在阶梯上
  assert.equal(preview.moreBelow, 0);
});

test("目标分辨率不在偏好序里 = 配置矛盾，安静返回 null", () => {
  assert.equal(
    upgradeLadderPreview({
      resolutions: ["1080p"],
      cutoff_resolution: "2160p",
      upgrade_source: "remux",
    }),
    null,
  );
});

test("未限定分辨率时按内置偏好序，终点缺省 1080p", () => {
  const preview = upgradeLadderPreview({ upgrade_source: "web-dl" });
  assert.equal(preview.target, "1080p · WEB-DL");
  assert.equal(preview.ceiling, "4320p · Remux");
  assert.deepEqual(preview.below.slice(0, 3), [
    "1080p · Rip 类",
    "1080p · 电视录制类",
    "720p · Remux",
  ]);
});
