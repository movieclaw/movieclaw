import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_SHARE_EXPIRY_DAYS,
  SHARE_EXPIRY_OPTIONS,
  absoluteShareUrl,
  expiryHint,
  generateSharePassword,
  isRelativeShareUrl,
  shareCopyText,
  sharePlayPath,
  validateSharePassword,
} from "../lib/share.ts";
import {
  localProgressKey,
  readLocalProgress,
  writeLocalProgress,
} from "../lib/player/local-progress.ts";

test("有效期只有四档、默认 7 天、没有永久", () => {
  assert.deepEqual(
    SHARE_EXPIRY_OPTIONS.map((o) => o.days),
    [1, 3, 7, 30],
  );
  assert.equal(DEFAULT_SHARE_EXPIRY_DAYS, 7);
});

test("访问码 6 位、只用不易混淆的小写字母数字", () => {
  const password = generateSharePassword();
  assert.match(password, /^[abcdefghijkmnpqrstuvwxyz23456789]{6}$/);
  // random 恒为 0.999… 时取字母表最后一位，不越界
  assert.equal(generateSharePassword(() => 0.9999999), "999999");
  assert.equal(generateSharePassword(() => 0), "aaaaaa");
});

test("密码校验：空 = 不设密码；长度越界给中文提示", () => {
  assert.equal(validateSharePassword("   "), null);
  assert.equal(validateSharePassword("k7pw2m"), null);
  assert.match(validateSharePassword("abc"), /4–32/);
  assert.match(validateSharePassword("x".repeat(33)), /4–32/);
});

test("相对链接用当前 origin 补全，绝对链接原样", () => {
  assert.equal(absoluteShareUrl("/s/abc", "http://nas.local:8000/"), "http://nas.local:8000/s/abc");
  assert.equal(absoluteShareUrl("https://x.example/s/abc", "http://nas.local"), "https://x.example/s/abc");
  assert.equal(isRelativeShareUrl("/s/abc"), true);
  assert.equal(isRelativeShareUrl("https://x.example/s/abc"), false);
});

test("复制文本：有密码带密码，无密码只有链接", () => {
  assert.equal(
    shareCopyText("沙丘 2", "https://x.example/s/abc", "k7pw2m"),
    "《沙丘 2》 链接：https://x.example/s/abc 密码：k7pw2m",
  );
  assert.equal(shareCopyText("沙丘 2", "https://x.example/s/abc", null), "《沙丘 2》 链接：https://x.example/s/abc");
});

test("到期提示按剩余时长换算", () => {
  const now = Date.parse("2026-09-06T12:00:00Z");
  assert.equal(expiryHint("2026-09-13T12:00:00Z", now), "7 天后失效");
  assert.equal(expiryHint("2026-09-08T12:00:00Z", now), "2 天后失效");
  assert.equal(expiryHint("2026-09-08T09:00:00Z", now), "45 小时后失效");
  assert.equal(expiryHint("2026-09-06T15:30:00Z", now), "3 小时后失效");
  assert.equal(expiryHint("2026-09-06T12:20:00Z", now), "20 分钟后失效");
  assert.equal(expiryHint("2026-09-06T11:00:00Z", now), "已失效");
});

test("分享页播放地址与 /play 同一套 sXXeYY + ?t= 约定", () => {
  assert.equal(sharePlayPath("abc"), "/s/abc/play");
  assert.equal(sharePlayPath("abc", { season: 1, episode: 3 }), "/s/abc/play/s01e03");
  assert.equal(sharePlayPath("abc", { season: 1, episode: 3, tSeconds: 95.7 }), "/s/abc/play/s01e03?t=95");
  assert.equal(sharePlayPath("a/b"), "/s/a%2Fb/play");
});

test("本地进度：按 slug 与单元隔离，停止不带位置记 0，轨记忆沿用上次", () => {
  const store = new Map();
  const storage = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
  };
  const unit = { media_item_id: 7, season_number: 1, episode_number: 2 };
  assert.equal(readLocalProgress("abc", unit, storage), null);
  writeLocalProgress("abc", unit, { position_ms: 65_000, audio_track: "embedded:1" }, storage, 1000);
  assert.deepEqual(readLocalProgress("abc", unit, storage), {
    position_ms: 65_000,
    audio_track: "embedded:1",
    subtitle_track: null,
    updated_at: 1000,
  });
  // 另一条分享 / 另一集互不影响
  assert.equal(readLocalProgress("xyz", unit, storage), null);
  assert.equal(readLocalProgress("abc", { ...unit, episode_number: 3 }, storage), null);
  // 停止上报不带位置 = 播到结尾：下次从头看；音轨记忆保留
  writeLocalProgress("abc", unit, {}, storage, 2000);
  assert.deepEqual(readLocalProgress("abc", unit, storage), {
    position_ms: 0,
    audio_track: "embedded:1",
    subtitle_track: null,
    updated_at: 2000,
  });
  // 坏数据当作没有记录
  store.set(localProgressKey("abc", unit), "not json");
  assert.equal(readLocalProgress("abc", unit, storage), null);
});
