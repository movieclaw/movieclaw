import assert from "node:assert/strict";
import test from "node:test";

import {
  clampSubtitleOffset,
  pickInitialSubtitle,
  plainCueText,
  planSubtitleTracks,
} from "../lib/player/subtitles.ts";
import { resolveShortcut } from "../lib/player/shortcuts.ts";

function plan(ref, kind, extra = {}) {
  return { track_ref: ref, kind, language: null, is_default: false, ...extra };
}

test("文本轨要 VTT，ASS 原样下发", () => {
  const { options } = planSubtitleTracks(
    [plan("external:zh.srt", "vtt"), plan("external:zh.ass", "ass")],
    ["/sub?track=1&token=t", "/sub?track=2&token=t"],
  );
  assert.equal(options[0].url, "/sub?track=1&token=t&format=vtt");
  // ASS 转 VTT 会丢掉特效与排版，番剧字幕直接崩——绝不能转
  assert.equal(options[1].url, "/sub?track=2&token=t");
});

test("内封轨与外挂轨一视同仁——PT 片源的字幕绝大多数是内封的", () => {
  const { options, unavailable } = planSubtitleTracks(
    [plan("embedded:0", "ass"), plan("embedded:1", "vtt")],
    ["/a", "/b"],
  );
  assert.equal(unavailable.length, 0);
  assert.equal(options.length, 2);
  assert.equal(options[0].kind, "ass");
  // ASS 不能转 VTT——转了就丢特效与排版，番剧字幕直接崩
  assert.equal(options[0].url, "/a");
  // 文本轨统一要 VTT：<track> 只认这个格式
  assert.equal(options[1].url, "/b&format=vtt");
});

test("AI 生成的轨要带标记，普通轨不带——两者可信度不一样", () => {
  const { options } = planSubtitleTracks(
    [plan("external:a.srt", "vtt", { is_ai: true }), plan("external:b.srt", "vtt")],
    ["/a", "/b"],
  );
  assert.equal(options[0].isAi, true);
  // 老版本服务端不带这个字段，缺省必须是 false 而不是 undefined
  assert.equal(options[1].isAi, false);
});

test("没有语言标记时内封轨按序号命名，不至于几条长得一样", () => {
  const { options } = planSubtitleTracks(
    [plan("embedded:0", "vtt"), plan("embedded:2", "vtt")],
    ["/a", "/b"],
  );
  assert.match(options[0].label, /内封轨 0/);
  assert.match(options[1].label, /内封轨 2/);
});

test("PGS 是可选轨：.sup 原样下发交 libbitsub，不追加 format 参数", () => {
  const { options, unavailable } = planSubtitleTracks(
    [plan("embedded:0", "pgs", { language: "chi" })],
    ["/sub?track=0&token=t"],
  );
  assert.equal(unavailable.length, 0);
  assert.equal(options.length, 1);
  assert.equal(options[0].kind, "pgs");
  assert.equal(options[0].url, "/sub?track=0&token=t");
  assert.match(options[0].label, /图形/);
});

test("未知格式（VobSub 等）仍进不可用清单并给出中文原因", () => {
  const { options, unavailable } = planSubtitleTracks(
    [plan("external:zh.sub", "vobsub")],
    ["/b"],
  );
  assert.equal(options.length, 0);
  assert.equal(unavailable.length, 1);
  assert.match(unavailable[0].reason, /暂不支持的字幕格式/);
});

test("字幕地址缺失时不做位移匹配", () => {
  // 错位下发的字幕比没有字幕更难排查
  const { options, unavailable } = planSubtitleTracks(
    [plan("external:a.srt", "vtt"), plan("external:b.srt", "vtt")],
    ["/only-one"],
  );
  assert.equal(options.length, 1);
  assert.equal(options[0].ref, "external:a.srt");
  assert.equal(unavailable.length, 1);
});

test("记住的轨优先于默认轨，off 表示用户明确关掉了字幕", () => {
  const { options } = planSubtitleTracks(
    [
      plan("external:zh.srt", "vtt", { is_default: true }),
      plan("external:en.srt", "vtt"),
    ],
    ["/a", "/b"],
  );
  assert.equal(pickInitialSubtitle(options, null), "external:zh.srt");
  assert.equal(pickInitialSubtitle(options, "external:en.srt"), "external:en.srt");
  assert.equal(pickInitialSubtitle(options, "off"), null);
  // 记住的轨这一集没有（换了片源）：回退到默认轨而不是留空
  assert.equal(pickInitialSubtitle(options, "external:ja.srt"), "external:zh.srt");
});

test("服务端没标默认轨就不自动开——不再兜底选第一条（与 Jellyfin 端同口径）", () => {
  const { options } = planSubtitleTracks(
    [plan("embedded:0", "vtt"), plan("embedded:1", "vtt")],
    ["/a", "/b"],
  );
  // is_default 全 false = 服务端裁决「不该自动开」（比如全是无旗标内封轨）；
  // 网页端硬开第一条就是两端对同一部片选出不同字幕的漂移来源
  assert.equal(pickInitialSubtitle(options, null), null);
});

test("时间轴微调钳到 ±30 秒并去掉浮点毛刺", () => {
  assert.equal(clampSubtitleOffset(0.1 + 0.2), 0.3);
  assert.equal(clampSubtitleOffset(99), 30);
  assert.equal(clampSubtitleOffset(-99), -30);
});

test("快捷键按 YouTube 惯例，输入框里一律不接管", () => {
  const press = (key, extra = {}) =>
    resolveShortcut({ key, inEditable: false, ...extra });
  assert.deepEqual(press(" "), { type: "toggle-play" });
  assert.deepEqual(press("k"), { type: "toggle-play" });
  assert.deepEqual(press("ArrowRight"), { type: "seek-by", seconds: 5 });
  assert.deepEqual(press("j"), { type: "seek-by", seconds: -10 });
  assert.deepEqual(press("5"), { type: "seek-percent", percent: 50 });
  assert.deepEqual(press("c"), { type: "toggle-subtitles" });

  assert.equal(resolveShortcut({ key: " ", inEditable: true }), null);
  // Cmd+← 是浏览器后退，抢走它的代价远大于收益
  assert.equal(press("ArrowLeft", { metaKey: true }), null);
});

test("cue 的富文本标签一律剥掉，保留文字", () => {
  assert.equal(plainCueText("<i>轻声地</i>说"), "轻声地说");
  assert.equal(plainCueText("<v Roger>你好</v>"), "你好");
});

test("cue 里的可执行标记不会被原样带出去", () => {
  // 字幕是用户丢进媒体库的任意文本，插进 DOM 前必须只剩纯文本
  assert.equal(plainCueText('<img src=x onerror="alert(1)">危险'), "危险");
  assert.equal(plainCueText("<script>alert(1)</script>"), "alert(1)");
});

test("多行 cue 的换行要留着", () => {
  assert.equal(plainCueText("第一行\n<b>第二行</b>"), "第一行\n第二行");
});
