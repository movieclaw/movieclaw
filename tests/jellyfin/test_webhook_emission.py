"""播放/收藏 HTTP 流程的 webhook 事件产生断言（docs/design/webhook.md §1.2）。

在协议层 emit 边界捕获事件（投递链路本身已由 tests/api/test_webhook_dispatcher.py
覆盖），验证：事件与 handler 的映射、stopped/completed 双发、级联 batch_id、
收藏层级翻译、client 信息透传、失败上报不产生事件。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jellyfin.helpers import jf_login
from movieclaw_api.services.playback import watch
from movieclaw_jellyfin.ids import episode_guid, item_guid, season_guid
from movieclaw_jellyfin.routes import playstate

TICKS_PER_MS = 10_000
RUNTIME_MS = 47 * 60 * 1000  # 播种剧集单集时长


@pytest.fixture
def emitted(monkeypatch) -> list:
    """捕获投递的全部事件（替换 emit_events，不走真实投递链路）。

    播放类事件由网页端与 Jellyfin 端共用的 watch 服务发出，标记/收藏类事件
    仍由协议路由自己发，两处都要截。
    """
    watch._progress_last_emit.clear()  # progress 节流状态在用例间隔离
    captured: list = []
    monkeypatch.setattr(watch, "emit_events", captured.extend)
    monkeypatch.setattr(playstate, "emit_events", captured.extend)
    return captured


def _auth(client: TestClient) -> dict:
    return {"ApiKey": jf_login(client)}


def test_started_event_with_client_info(client, seeded, emitted):
    auth = _auth(client)
    ep = episode_guid(seeded["show"], 1, 1)
    assert (
        client.post("/Sessions/Playing", params=auth, json={"ItemId": ep}).status_code
        == 204
    )
    assert [e.event for e in emitted] == ["playback.started"]
    event = emitted[0]
    media = event.data["media"]
    assert media["type"] == "episode"
    assert media["tmdb_id"] == 1396
    assert (media["season_number"], media["episode_number"]) == (1, 1)
    assert event.data["playback"]["play_count"] == 1
    # client 信息来自登录时注册的设备（helpers.AUTH_HEADER）
    assert event.data["client"]["name"] == "Infuse"
    assert event.data["client"]["device_name"] == "Apple TV"


def test_progress_events_and_throttle(client, seeded, emitted):
    auth = _auth(client)
    ep = episode_guid(seeded["show"], 1, 1)
    # 40%：普通进度 → 发一条 progress
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": int(RUNTIME_MS * 0.4) * TICKS_PER_MS},
    )
    assert [e.event for e in emitted] == ["playback.progress"]
    assert emitted[0].data["media"]["episode_title"] == "Pilot"
    # 30 秒内再报进度：被节流，不产生事件
    emitted.clear()
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": RUNTIME_MS // 2 * TICKS_PER_MS},
    )
    assert emitted == []
    # 95%：仅靠 Progress 翻转已看 → 只发 completed（progress 被 completed 抑制）
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": int(RUNTIME_MS * 0.95) * TICKS_PER_MS},
    )
    assert [e.event for e in emitted] == ["playback.completed"]
    assert emitted[0].data["playback"]["played"] is True
    assert emitted[0].data["playback"]["duration_ms"] == RUNTIME_MS


def test_stopped_at_end_emits_stopped_and_completed(client, seeded, emitted):
    auth = _auth(client)
    ep = episode_guid(seeded["show"], 2, 1)
    client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": ep, "PositionTicks": int(RUNTIME_MS * 0.95) * TICKS_PER_MS},
    )
    assert [e.event for e in emitted] == ["playback.stopped", "playback.completed"]
    # 已看状态下再次停止：只发 stopped，不重复 completed
    emitted.clear()
    client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": ep, "PositionTicks": int(RUNTIME_MS * 0.95) * TICKS_PER_MS},
    )
    assert [e.event for e in emitted] == ["playback.stopped"]


def test_stopped_failed_emits_nothing(client, seeded, emitted):
    auth = _auth(client)
    movie = item_guid(seeded["movie"])
    client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": movie, "PositionTicks": 999 * TICKS_PER_MS, "Failed": True},
    )
    assert emitted == []


def test_legacy_start_route_emits(client, seeded, emitted):
    auth = _auth(client)
    movie = item_guid(seeded["movie"])
    assert client.post(f"/PlayingItems/{movie}", params=auth).status_code == 204
    assert [e.event for e in emitted] == ["playback.started"]
    media = emitted[0].data["media"]
    assert media["type"] == "movie"
    assert media["season_number"] is None and media["episode_number"] is None


def test_mark_played_cascade_shares_batch_id(client, seeded, emitted):
    auth = _auth(client)
    show = item_guid(seeded["show"])
    assert client.post(f"/UserPlayedItems/{show}", params=auth).status_code == 200
    # 播种了 3 集 → 级联 3 条事件，共享同一 batch_id
    assert [e.event for e in emitted] == ["playback.marked_played"] * 3
    batch_ids = {e.batch_id for e in emitted}
    assert len(batch_ids) == 1 and None not in batch_ids

    emitted.clear()
    client.request("DELETE", f"/UserPlayedItems/{show}", params=auth)
    assert [e.event for e in emitted] == ["playback.marked_unplayed"] * 3


def test_favorite_levels_no_sentinel_leak(client, seeded, emitted):
    auth = _auth(client)
    season = season_guid(seeded["show"], 2)
    show = item_guid(seeded["show"])

    client.post(f"/UserFavoriteItems/{season}", params=auth)
    client.post(f"/UserFavoriteItems/{show}", params=auth)
    assert [e.event for e in emitted] == ["item.favorited", "item.favorited"]
    season_media = emitted[0].data["media"]
    assert season_media["type"] == "season"
    assert season_media["season_number"] == 2
    assert season_media["episode_number"] is None  # 哨兵 -1 不外泄
    series_media = emitted[1].data["media"]
    assert series_media["type"] == "series"
    assert series_media["season_number"] is None

    emitted.clear()
    client.request("DELETE", f"/UserFavoriteItems/{show}", params=auth)
    assert [e.event for e in emitted] == ["item.unfavorited"]
    assert emitted[0].data["favorite"] is False
