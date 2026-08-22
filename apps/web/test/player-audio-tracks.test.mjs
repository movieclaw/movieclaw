import assert from "node:assert/strict";
import test from "node:test";

import { audioTrackLabel, planAudioOptions } from "../lib/player/audio-tracks.ts";

const track = (over = {}) => ({
  ref: "embedded:0",
  codec: "aac",
  channels: 2,
  language: "chi",
  is_default: false,
  ...over,
});

test("显示名是「语言 · 编码 · 声道」，语言在最前", () => {
  assert.equal(audioTrackLabel(track({ language: "jpn", codec: "eac3", channels: 6 })), "日语 · EAC3 · 5.1");
});

test("声道用通用写法，不写「6 声道」", () => {
  assert.equal(audioTrackLabel(track({ channels: 6 })), "中文 · AAC · 5.1");
  assert.equal(audioTrackLabel(track({ channels: 8 })), "中文 · AAC · 7.1");
  assert.equal(audioTrackLabel(track({ channels: 2 })), "中文 · AAC · 立体声");
  assert.equal(audioTrackLabel(track({ channels: 1 })), "中文 · AAC · 单声道");
  // 认不出的声道数照实写，总比省掉强
  assert.equal(audioTrackLabel(track({ channels: 3 })), "中文 · AAC · 3 声道");
});

test("没有语言标记时退回序号，不能都叫「未知语言」", () => {
  // 否则多条无标记的轨在菜单里长得一模一样，等于没得选
  assert.equal(audioTrackLabel(track({ language: null, ref: "embedded:2" })), "音轨 2 · AAC · 立体声");
  assert.equal(audioTrackLabel(track({ language: "und", ref: "embedded:3" })), "音轨 3 · AAC · 立体声");
});

test("编码与声道都缺时只剩语言，不留下孤零零的分隔点", () => {
  assert.equal(audioTrackLabel(track({ codec: null, channels: null })), "中文");
});

test("只有一条音轨时不给菜单——没得选的菜单是纯噪音", () => {
  assert.deepEqual(planAudioOptions([track()]), []);
  assert.deepEqual(planAudioOptions([]), []);
  assert.deepEqual(planAudioOptions(undefined), []);
});

test("两条以上才成菜单，顺序跟服务端一致", () => {
  const options = planAudioOptions([
    track({ ref: "embedded:0", language: "jpn", is_default: true }),
    track({ ref: "embedded:1", language: "chi" }),
  ]);
  assert.deepEqual(
    options.map((o) => [o.ref, o.label, o.isDefault]),
    [
      ["embedded:0", "日语 · AAC · 立体声", true],
      ["embedded:1", "中文 · AAC · 立体声", false],
    ],
  );
});
