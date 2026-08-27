import assert from "node:assert/strict";
import test from "node:test";

import {
  groupTodayArrivals,
  subscriptionCollectionMeta,
  subscriptionFullyCollected,
  subscriptionRibbon,
  todayArrivalPresentation,
} from "../lib/subscription-ui.ts";

function subscription({
  kind = "tv",
  followFuture = true,
  wanted = 0,
  grabbed = 0,
  downloaded = 0,
  imported = 0,
  upgrading = 0,
  status = "active",
  mediaStatus = "Returning Series",
  selectedSeasons = [],
  seasons = [],
} = {}) {
  return {
    media: { kind, status: mediaStatus },
    status,
    selected_seasons: selectedSeasons,
    follow_future: followFuture,
    season_collection: seasons,
    progress: {
      total: wanted + grabbed + downloaded + imported,
      wanted,
      grabbed,
      downloaded,
      imported,
      upgrading,
    },
  };
}

test("追新中的剧集角标不随工单阶段变化，仍是自动续订", () => {
  for (const progress of [{ wanted: 2 }, { grabbed: 1 }, { downloaded: 1 }, { imported: 12 }]) {
    assert.deepEqual(subscriptionRibbon(subscription(progress)), {
      label: "自动续订",
      tone: "subscribed",
    });
  }
});

test("创建时内容已在库且没有工单也显示自动续订", () => {
  assert.deepEqual(subscriptionRibbon(subscription()), {
    label: "自动续订",
    tone: "subscribed",
  });
});

test("关闭自动续订、又没收齐的订阅不显示角标", () => {
  assert.equal(subscriptionRibbon(subscription({ followFuture: false })), undefined);
  assert.equal(subscriptionRibbon(subscription({ kind: "movie", wanted: 1 })), undefined);
});

// --- issue #221：订阅墙要一眼看出哪些已经到手 ---

test("已入库的电影订阅打「已入库」角标", () => {
  assert.deepEqual(subscriptionRibbon(subscription({ kind: "movie", imported: 1 })), {
    label: "已入库",
    tone: "owned",
  });
});

test("电影只要还没入库就不打已入库，哪怕已经在下载", () => {
  assert.equal(subscriptionRibbon(subscription({ kind: "movie", downloaded: 1 })), undefined);
  assert.equal(subscriptionRibbon(subscription({ kind: "movie", grabbed: 1 })), undefined);
});

test("收齐的完结剧说「已收齐」，并压过自动续订", () => {
  assert.deepEqual(
    subscriptionRibbon(
      subscription({ status: "completed", mediaStatus: "Ended", imported: 24 }),
    ),
    { label: "已收齐", tone: "owned" },
  );
});

test("追新剧永远不算收齐——它确实还没完", () => {
  // 已经入库 12 集，但订阅仍在追新（status=active）：不能盖上"到手了"的章
  assert.deepEqual(subscriptionRibbon(subscription({ imported: 12 })), {
    label: "自动续订",
    tone: "subscribed",
  });
  assert.equal(subscriptionFullyCollected(subscription({ imported: 12 })), false);
});

test("状态是 completed 但一集都没入库时不算收齐", () => {
  assert.equal(
    subscriptionFullyCollected(subscription({ status: "completed", imported: 0 })),
    false,
  );
});

function season(seasonNumber, episodeCount, airedCount, ownedCount) {
  return {
    season_number: seasonNumber,
    name: `第 ${seasonNumber} 季`,
    air_date: null,
    episode_count: episodeCount,
    aired_count: airedCount,
    owned_count: ownedCount,
  };
}

test("在播剧只展示最新一季的低密度收录进度", () => {
  const result = subscriptionCollectionMeta(
    subscription({
      selectedSeasons: [1, 2],
      seasons: [season(1, 10, 10, 10), season(2, 7, 5, 5)],
    }),
  );
  assert.deepEqual(result, {
    label: "第 2 季",
    value: "5 / 7",
    tracking: true,
    upgrading: false,
    upgradingCount: undefined,
  });
});

test("完结多季已收齐时聚合为季数和总集数", () => {
  const seasons = Array.from({ length: 8 }, (_, index) => {
    const count = index === 7 ? 10 : 9;
    return season(index + 1, count, count, count);
  });
  const result = subscriptionCollectionMeta(
    subscription({
      status: "completed",
      mediaStatus: "Ended",
      selectedSeasons: seasons.map((row) => row.season_number),
      seasons,
    }),
  );
  assert.deepEqual(result, {
    label: "全 8 季",
    value: "全 73 集",
    tracking: false,
    upgrading: false,
    upgradingCount: undefined,
  });
});

test("完结剧补旧时聚合显示已收录和总集数", () => {
  const result = subscriptionCollectionMeta(
    subscription({
      mediaStatus: "Ended",
      selectedSeasons: [1, 2, 3],
      seasons: [
        season(1, 10, 10, 8),
        season(2, 10, 10, 8),
        season(3, 10, 10, 8),
        season(4, 10, 10, 10),
      ],
    }),
  );
  assert.deepEqual(result, {
    label: "共 3 季",
    value: "24 / 30",
    tracking: false,
    upgrading: false,
    upgradingCount: undefined,
  });
});

test("最新一季尚未播出时不展示零进度", () => {
  const result = subscriptionCollectionMeta(
    subscription({
      selectedSeasons: [3],
      seasons: [season(3, 10, 0, 0)],
    }),
  );
  assert.deepEqual(result, {
    label: "第 3 季",
    value: "待播出",
    tracking: true,
    upgrading: false,
    upgradingCount: undefined,
  });
});

test("洗版中的订阅亮青点，暂停后不亮", () => {
  const seasons = [season(1, 10, 10, 10)];
  const upgrading = subscriptionCollectionMeta(
    subscription({ selectedSeasons: [1], seasons, upgrading: 1 }),
  );
  assert.equal(upgrading.upgrading, true);

  const paused = subscriptionCollectionMeta(
    subscription({ selectedSeasons: [1], seasons, upgrading: 1, status: "paused" }),
  );
  assert.equal(paused.upgrading, false);
});

test("追更绿点只由未完结订阅的 active 状态控制", () => {
  const paused = subscriptionCollectionMeta(
    subscription({
      status: "paused",
      selectedSeasons: [2],
      seasons: [season(2, 7, 5, 5)],
    }),
  );
  const disabled = subscriptionCollectionMeta(
    subscription({
      followFuture: false,
      selectedSeasons: [2],
      seasons: [season(2, 7, 5, 5)],
    }),
  );

  assert.equal(paused.tracking, false);
  assert.equal(disabled.tracking, true);
});

test("电影不展示收录摘要", () => {
  assert.equal(
    subscriptionCollectionMeta(subscription({ kind: "movie", seasons: [season(1, 1, 1, 1)] })),
    undefined,
  );
});

function todayArrival(overrides = {}) {
  return {
    subscription_id: 1,
    wanted_id: 10,
    media_title: "测试剧集",
    media_kind: "tv",
    season_number: 2,
    episode_number: 5,
    status: "wanted",
    air_date: "2030-01-01",
    expected_day: "2030-01-01",
    days_ahead: 0,
    release_forecast: null,
    next_probe_at: null,
    info_hash: null,
    grabbed_at: null,
    downloaded_at: null,
    estimated_release_to_import_minutes: 60,
    estimated_download_to_import_minutes: 10,
    ...overrides,
  };
}

test("待播集只展示按历史耗时换算后的预计入库时间", () => {
  const now = new Date("2030-01-01T10:00:00Z");
  const predictedAt = "2030-01-01T12:00:00Z";
  const result = todayArrivalPresentation(
    todayArrival({
      release_forecast: {
        predicted_at: predictedAt,
        window_end: "2030-01-01T14:00:00Z",
      },
    }),
    undefined,
    now,
  );
  const expectedClock = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date("2030-01-01T13:00:00Z"));
  assert.equal(result.statusLabel, "预计入库");
  assert.equal(result.timeLabel, `约 ${expectedClock}`);
});

test("首个预计入库时间过期后滚动到下一探测点", () => {
  const now = new Date("2030-01-01T13:10:00Z");
  const result = todayArrivalPresentation(
    todayArrival({
      release_forecast: {
        predicted_at: "2030-01-01T12:00:00Z",
        window_end: "2030-01-01T14:00:00Z",
      },
      next_probe_at: "2030-01-01T13:30:00Z",
    }),
    undefined,
    now,
  );

  assert.equal(result.statusLabel, "等待资源");
  assert.equal(result.estimatedAt, Date.parse("2030-01-01T14:30:00Z"));
});

test("所有探测点对应的入库时间都过期后不展示旧时间", () => {
  const result = todayArrivalPresentation(
    todayArrival({
      release_forecast: {
        predicted_at: "2030-01-01T12:00:00Z",
        window_end: "2030-01-01T14:00:00Z",
      },
    }),
    undefined,
    new Date("2030-01-01T14:00:00Z"),
  );

  assert.equal(result.statusLabel, "等待资源");
  assert.equal(result.timeLabel, "时间待更新");
  assert.equal(result.estimatedAt, null);
});

test("下载中不展示进度，只用实时 ETA 更新预计入库时间", () => {
  const now = new Date("2030-01-01T10:00:00Z");
  const result = todayArrivalPresentation(
    todayArrival({ status: "grabbed", info_hash: "abc" }),
    { state: "downloading", eta_seconds: 20 * 60 },
    now,
  );
  assert.equal(result.statusLabel, "下载中");
  assert.equal(result.estimatedAt, now.getTime() + 30 * 60 * 1000);
  assert.equal("progress" in result, false);
});

test("下载完成后首页收敛为整理中", () => {
  const result = todayArrivalPresentation(todayArrival({ status: "downloaded" }));
  assert.equal(result.statusLabel, "整理中");
  assert.equal(result.timeLabel, "即将完成");
});

function presented(
  arrival,
  presentation = { statusLabel: "预计入库", timeLabel: "时间待更新", estimatedAt: null },
) {
  return { arrival, presentation };
}

test("同一部剧同日连续更新多集时合并为一个范围", () => {
  const groups = groupTodayArrivals([
    presented(todayArrival({ wanted_id: 16, episode_number: 16 })),
    presented(todayArrival({ wanted_id: 17, episode_number: 17 })),
    presented(todayArrival({ wanted_id: 20, episode_number: 20 })),
    presented(todayArrival({ wanted_id: 18, episode_number: 18 })),
    presented(todayArrival({ wanted_id: 19, episode_number: 19 })),
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].episodeLabel, "S02E16–E20");
  assert.equal(groups[0].episodeCount, 5);
});

test("非连续集数和跨季更新不会被误写成连续范围", () => {
  const groups = groupTodayArrivals([
    presented(todayArrival({ episode_number: 16 })),
    presented(todayArrival({ wanted_id: 12, episode_number: 18 })),
    presented(todayArrival({ wanted_id: 13, season_number: 3, episode_number: 1 })),
    presented(todayArrival({ wanted_id: 14, season_number: 3, episode_number: 2 })),
  ]);

  assert.equal(groups[0].episodeLabel, "S02E16、E18 · S03E01–E02");
});

test("合并行使用尚未完成且预计最晚的一集作为整体状态", () => {
  const groups = groupTodayArrivals([
    presented(todayArrival({ wanted_id: 10, status: "downloaded" }), {
      statusLabel: "整理中",
      timeLabel: "即将完成",
      estimatedAt: 100,
    }),
    presented(todayArrival({ wanted_id: 11, episode_number: 6, status: "grabbed" }), {
      statusLabel: "下载中",
      timeLabel: "约 20:00",
      estimatedAt: 200,
    }),
    presented(todayArrival({ wanted_id: 12, episode_number: 7 }), {
      statusLabel: "等待资源",
      timeLabel: "时间待更新",
      estimatedAt: null,
    }),
  ]);

  assert.equal(groups[0].presentation.statusLabel, "等待资源");
  assert.equal(groups[0].presentation.timeLabel, "时间待更新");
});

test("给不出入库时刻时优先报下次探测时刻，让用户看到系统在动", () => {
  const result = todayArrivalPresentation(
    todayArrival({
      release_forecast: {
        predicted_at: "2030-01-01T12:00:00Z",
        window_end: "2030-01-01T14:00:00Z",
        confidence: "volatile",
      },
      next_probe_at: "2030-01-01T13:30:00Z",
    }),
    undefined,
    new Date("2030-01-01T10:00:00Z"),
  );
  const expectedClock = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date("2030-01-01T13:30:00Z"));

  assert.equal(result.estimatedAt, null);
  assert.equal(result.timeLabel, `${expectedClock} 探测`);
});

test("未来预告没有可用时刻时退到播出日期，而不是一句时间待更新", () => {
  const result = todayArrivalPresentation(
    todayArrival({ expected_day: "2030-01-04", days_ahead: 3 }),
    undefined,
    new Date("2030-01-01T10:00:00Z"),
  );

  assert.equal(result.statusLabel, "预计入库");
  assert.equal(result.timeLabel, "1月4日 播出");
});

test("几天后的预告不写只有时分的探测时刻，避免被当成今天", () => {
  const result = todayArrivalPresentation(
    todayArrival({
      expected_day: "2030-01-04",
      days_ahead: 3,
      next_probe_at: "2030-01-04T13:30:00Z",
    }),
    undefined,
    new Date("2030-01-01T10:00:00Z"),
  );

  assert.equal(result.timeLabel, "1月4日 播出");
});

test("电影不展示 S00E00 哨兵季集号", () => {
  const groups = groupTodayArrivals([
    presented(
      todayArrival({
        media_kind: "movie",
        media_title: "测试电影",
        season_number: 0,
        episode_number: 0,
        status: "grabbed",
      }),
      { statusLabel: "下载中", timeLabel: "约 20:00", estimatedAt: 200 },
    ),
  ]);

  assert.equal(groups[0].episodeLabel, "电影");
  assert.equal(groups[0].daysAhead, 0);
});
