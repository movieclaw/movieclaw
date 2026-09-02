import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MEDIA_CARDS_TOOL_V1,
  isMediaCardsTool,
  parseMediaCardsArgs,
} from "../lib/agent-media-cards.ts";

test("只有带版本的工具名才由卡片渲染器负责", () => {
  assert.equal(isMediaCardsTool(MEDIA_CARDS_TOOL_V1), true);
  assert.equal(isMediaCardsTool("render_media_cards"), false);
  assert.equal(isMediaCardsTool("render_media_cards_v2"), false);
  assert.equal(isMediaCardsTool("mclaw"), false);
  // 未知版本返回 null：会话页退回普通工具行，不会整轮渲染失败
  assert.equal(
    parseMediaCardsArgs("render_media_cards_v2", { component: "library", items: [{ library_id: 1 }] }),
    null,
  );
});

test("media library 卡片按 library_id 解析，非法项跳过、重复项去重", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "library",
    title: " 电影库 ",
    items: [{ library_id: 3 }, { library_id: "3" }, { library_id: 0 }, null, { library_id: 3 }, { library_id: 5 }],
  });
  assert.deepEqual(group, {
    component: "library",
    title: "电影库",
    cards: [
      { kind: "library", key: "library:3", libraryId: 3 },
      { kind: "library", key: "library:5", libraryId: 5 },
    ],
  });
});

test("title 卡片：tmdb_id+media_type 或 douban_id 拼成服务端 title_ref", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "title",
    items: [
      { tmdb_id: 693134, media_type: "movie" },
      { tmdb_id: 1399, media_type: "tv" },
      { tmdb_id: 7, media_type: "anime" },
      { tmdb_id: 8 },
      { douban_id: " 1292052 " },
      { douban_id: 26752088 },
      { douban_id: "bad:ref" },
      {},
    ],
  });
  assert.ok(group);
  assert.deepEqual(
    group.cards.map((c) => c.titleRef),
    ["tmdb:movie:693134", "tmdb:tv:1399", "douban:1292052", "douban:26752088"],
  );
  assert.equal(group.cards[0].source, "tmdb");
  assert.equal(group.cards[0].mediaType, "movie");
  assert.equal(group.cards[2].source, "douban");
  assert.equal(group.cards[2].mediaType, undefined);
  assert.equal(group.cards[2].externalId, "1292052");
});

test("library_item 卡片：季集成对才生效，缺一半按整部处理", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "library_item",
    items: [
      { media_item_id: 42, season: 1, episode: 3 },
      { media_item_id: 42, season: 2 },
      { media_item_id: 42, season: 0, episode: 1 },
      { media_item_id: -1 },
    ],
  });
  assert.ok(group);
  assert.equal(group.cards.length, 3);
  assert.deepEqual(group.cards[0], {
    kind: "library_item",
    key: "item:42:s1e3:0",
    mediaItemId: 42,
    season: 1,
    episode: 3,
  });
  assert.equal(group.cards[1].season, undefined);
  assert.equal(group.cards[1].episode, undefined);
  assert.equal(group.cards[2].season, 0);
});

test("整组无一张可画时返回 null；未知组件返回 null", () => {
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, { component: "library", items: [{}] }), null);
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, { component: "chart", items: [{ id: 1 }] }), null);
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, undefined), null);
});
