import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MEDIA_CARDS_TOOL_V1,
  isMediaCardsTool,
  parseMediaCardsArgs,
} from "../lib/agent-media-cards.ts";

test("只有带版本的工具名才由卡片渲染器负责", () => {
  assert.equal(isMediaCardsTool(MEDIA_CARDS_TOOL_V1), true);
  assert.equal(isMediaCardsTool("show_media_cards"), false);
  assert.equal(isMediaCardsTool("show_media_cards_v2"), false);
  assert.equal(isMediaCardsTool("mclaw"), false);
  // 未知版本返回 null：会话页退回普通工具行，不会整轮渲染失败
  assert.equal(
    parseMediaCardsArgs("show_media_cards_v2", { component: "library", items: [{ library_id: 1 }] }),
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

test("title 卡片：首选 mclaw 的 title_ref，只有 tmdb_id+media_type 时拼成同一形态", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "title",
    items: [
      { title_ref: "tmdb:movie:693134" },
      { title_ref: " douban:1292052 " },
      { tmdb_id: 1399, media_type: "tv" },
      { title_ref: "tmdb:movie:693134" },
      { title_ref: "693134" },
      { title_ref: "tmdb:anime:7" },
      { tmdb_id: 8 },
      { tmdb_id: 9, media_type: "anime" },
      {},
    ],
  });
  assert.ok(group);
  assert.deepEqual(
    group.cards.map((c) => c.titleRef),
    ["tmdb:movie:693134", "douban:1292052", "tmdb:tv:1399"],
  );
  assert.equal(group.cards[0].key, "tmdb:movie:693134");
});

test("library_item 卡片：season_number/episode_number 成对才生效，缺一半按整部处理", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "library_item",
    items: [
      { media_item_id: 42, season_number: 1, episode_number: 3 },
      { media_item_id: 42, season_number: 2 },
      { media_item_id: 42, season_number: 0, episode_number: 1 },
      { media_item_id: -1 },
    ],
  });
  assert.ok(group);
  assert.equal(group.cards.length, 3);
  assert.deepEqual(group.cards[0], {
    kind: "library_item",
    key: "item:42:s1e3:0",
    mediaItemId: 42,
    seasonNumber: 1,
    episodeNumber: 3,
  });
  assert.equal(group.cards[1].seasonNumber, undefined);
  assert.equal(group.cards[1].episodeNumber, undefined);
  assert.equal(group.cards[2].seasonNumber, 0);
});

test("subscription 卡片按 subscription_id 解析", () => {
  const group = parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, {
    component: "subscription",
    items: [{ subscription_id: 12 }, { subscription_id: "12" }, { subscription_id: 12 }],
  });
  assert.deepEqual(group, {
    component: "subscription",
    title: undefined,
    cards: [{ kind: "subscription", key: "subscription:12", subscriptionId: 12 }],
  });
});

test("整组无一张可画时返回 null；未知组件返回 null", () => {
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, { component: "library", items: [{}] }), null);
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, { component: "chart", items: [{ id: 1 }] }), null);
  assert.equal(parseMediaCardsArgs(MEDIA_CARDS_TOOL_V1, undefined), null);
});
