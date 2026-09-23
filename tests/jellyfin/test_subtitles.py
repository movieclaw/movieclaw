"""外挂字幕全链路（docs/design/jellyfin-subtitle.md S2/S3 验收）。

PlaybackInfo 投递字段 → Stream 接口拉取（GBK→UTF-8 / srt→vtt）→
轨记忆（含 -1=明确关闭）→ 成员库可见性。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from jellyfin.helpers import jf_login
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.models import LibraryFile
from movieclaw_jellyfin.ids import item_guid

# 电影主文件 stem = Inception.2010.2160p（conftest 播种）
SIDECAR_NAME = "Inception.2010.2160p.chs.default.srt"
SRT_GBK = (
    "1\n00:00:01,000 --> 00:00:02,500\n简体中文字幕测试\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n第二行\n\n"
)
# Jellyfin 官方合成编号：外挂=0、video=1、audio=2、内封字幕=3（§4.1）
EXTERNAL_INDEX = 0
AUDIO_INDEX = 2
EMBEDDED_INDEX = 3


@pytest.fixture
def subtitle_env(seeded: dict, media_root: Path) -> dict:
    """播种外挂字幕：写 GBK sidecar 文件 + 台账 external_subtitles。"""
    from movieclaw_api.core.config import get_settings

    sidecar = media_root / "Inception (2010)" / SIDECAR_NAME
    sidecar.write_bytes(SRT_GBK.encode("gbk"))
    st = sidecar.stat()

    async def _update() -> None:
        init_db(get_settings().database_url, echo=False)
        async with get_database().session() as session:
            row = (
                await session.execute(
                    select(LibraryFile).where(
                        LibraryFile.file_path.like("%Inception.2010.2160p.mkv")
                    )
                )
            ).scalar_one()
            # title=None：chs/default 两个 token 都被语言/旗标消费（与
            # discover_external_subtitles 的真实产物一致）
            row.external_subtitles = [
                {
                    "filename": SIDECAR_NAME,
                    "format": "srt",
                    "language": "chi",
                    "title": None,
                    "default": True,
                    "forced": False,
                    "sdh": False,
                    "size_bytes": st.st_size,
                    "file_mtime_ns": st.st_mtime_ns,
                }
            ]
            await session.commit()
        await dispose_db()

    asyncio.run(_update())
    return seeded


@pytest.fixture
def sclient(subtitle_env: dict, client: TestClient) -> TestClient:
    """字幕播种在应用启动前完成（fixture 依赖顺序保证）。"""
    return client


def _playback_info(sclient: TestClient, token: str, guid: str) -> dict:
    resp = sclient.post(
        f"/Items/{guid}/PlaybackInfo", headers={"X-Emby-Token": token}, json={}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_ai_subtitle_display_title_carries_marker() -> None:
    """AI 生成字幕（title="ai"）在轨列表里可辨识（subtitle-ai-translate.md §0）。"""
    from movieclaw_jellyfin.catalog import _external_subtitle_stream

    stream = _external_subtitle_stream(
        {"filename": "M.chs.ai.srt", "format": "srt", "language": "chi", "title": "ai"},
        3,
        Path("/media"),
    )
    assert stream["DisplayTitle"] == "Chinese - ai - SUBRIP - External"
    # 语言解析不出时 title 顶格，不重复出现
    stream = _external_subtitle_stream(
        {"filename": "M.简中特效.srt", "format": "srt", "language": None, "title": "简中特效"},
        3,
        Path("/media"),
    )
    assert stream["DisplayTitle"] == "简中特效 - SUBRIP - External"


def test_playback_info_external_subtitle_stream(sclient: TestClient, subtitle_env: dict) -> None:
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    body = _playback_info(sclient, token, guid)
    source = body["MediaSources"][0]

    subs = [s for s in source["MediaStreams"] if s["Type"] == "Subtitle"]
    assert [(s["Index"], s["IsExternal"]) for s in subs] == [
        (EXTERNAL_INDEX, True),
        (EMBEDDED_INDEX, False),
    ]

    external = subs[0]
    assert external["IsExternal"] is True
    assert external["Codec"] == "subrip"
    assert external["Language"] == "chi"
    assert external["IsDefault"] is True
    assert external["SupportsExternalStream"] is True
    assert external["IsTextSubtitleStream"] is True
    assert external["Path"] == str(Path(source["Path"]).with_name(SIDECAR_NAME))
    assert external["DisplayTitle"] == "Chinese - Default - SUBRIP - External"
    # 投递字段（仅 PlaybackInfo 场景，无条件输出）
    assert external["DeliveryMethod"] == "External"
    assert external["IsExternalUrl"] is False
    assert external["DeliveryUrl"] == (
        f"/Videos/{guid}/{source['Id']}/Subtitles/{EXTERNAL_INDEX}/0/Stream.srt"
        f"?ApiKey={token}"
    )
    # 内封字幕流不带投递字段（DirectPlay 播放器自解，对齐真 Jellyfin）
    assert "DeliveryMethod" not in subs[1]
    # 默认轨：无记忆时选择策略生效——外挂优先
    assert source["DefaultSubtitleStreamIndex"] == EXTERNAL_INDEX


def test_subtitle_stream_gbk_to_utf8(sclient: TestClient, subtitle_env: dict) -> None:
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    source = _playback_info(sclient, token, guid)["MediaSources"][0]
    url = next(
        s["DeliveryUrl"]
        for s in source["MediaStreams"]
        if s["Type"] == "Subtitle" and s.get("IsExternal")
    )
    # DeliveryUrl 自带 ApiKey，不带任何认证头直接拉（播放器媒体内核形态）
    resp = sclient.get(url)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-subrip")
    text = resp.content.decode("utf-8")
    assert "简体中文字幕测试" in text  # GBK 源 → UTF-8 输出


def test_subtitle_stream_vtt_conversion(sclient: TestClient, subtitle_env: dict) -> None:
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    source = _playback_info(sclient, token, guid)["MediaSources"][0]
    ms_id = source["Id"]

    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/0/Stream.vtt",
        params={"ApiKey": jf_login(sclient)},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")
    text = resp.content.decode("utf-8")
    assert text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in text
    assert "简体中文字幕测试" in text


def test_subtitle_stream_ticksless_route_and_format_override(
    sclient: TestClient, subtitle_env: dict
) -> None:
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    ms_id = _playback_info(sclient, token, guid)["MediaSources"][0]["Id"]
    # 不带 ticks 的路由 + query format 覆盖（对齐 ParameterObsolete 行为）
    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/Stream.srt",
        params={"ApiKey": token, "format": "vtt"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")


@pytest.mark.parametrize(
    ("requested_format", "content_type", "expected_prefix"),
    [
        ("subrip", "application/x-subrip", b"1\n"),
        ("webvtt", "text/vtt", b"WEBVTT"),
    ],
)
def test_subtitle_stream_accepts_jellyfin_codec_aliases(
    sclient: TestClient,
    subtitle_env: dict,
    requested_format: str,
    content_type: str,
    expected_prefix: bytes,
) -> None:
    """VidHub 会按 MediaStream.Codec 构造无 ticks 的字幕地址。"""
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    ms_id = _playback_info(sclient, token, guid)["MediaSources"][0]["Id"]

    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/Stream.{requested_format}",
        params={"ApiKey": token},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(content_type)
    assert resp.content.startswith(expected_prefix)
    assert "简体中文字幕测试" in resp.content.decode("utf-8")


def test_subtitle_stream_embedded_index_404(sclient: TestClient, subtitle_env: dict) -> None:
    """内封轨抽取失败（假文件 ffmpeg 读不出）：404 空 body，不泄露任何细节。"""
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    ms_id = _playback_info(sclient, token, guid)["MediaSources"][0]["Id"]
    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EMBEDDED_INDEX}/0/Stream.srt",
        params={"ApiKey": token},
    )
    assert resp.status_code == 404
    assert resp.content == b""


def test_subtitle_stream_requires_token(sclient: TestClient, subtitle_env: dict) -> None:
    """偏离③：真 Jellyfin 此接口匿名，我们要求 token。"""
    guid = item_guid(subtitle_env["movie"])
    token = jf_login(sclient)
    ms_id = _playback_info(sclient, token, guid)["MediaSources"][0]["Id"]
    resp = sclient.get(f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/0/Stream.srt")
    assert resp.status_code == 401


def test_track_memory_roundtrip(sclient: TestClient, subtitle_env: dict) -> None:
    """切轨 → 记忆 → 下次 PlaybackInfo 沿用；-1（明确关闭）同样记住。"""
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    headers = {"X-Emby-Token": token}
    ms_id = _playback_info(sclient, token, guid)["MediaSources"][0]["Id"]

    # 上报选择了内封字幕轨 + 第一条音轨
    resp = sclient.post(
        "/Sessions/Playing/Progress",
        headers=headers,
        json={
            "ItemId": guid,
            "MediaSourceId": ms_id,
            "PositionTicks": 60 * 10_000_000,
            "AudioStreamIndex": AUDIO_INDEX,
            "SubtitleStreamIndex": EMBEDDED_INDEX,
        },
    )
    assert resp.status_code == 204
    source = _playback_info(sclient, token, guid)["MediaSources"][0]
    assert source["DefaultSubtitleStreamIndex"] == EMBEDDED_INDEX  # 记忆优先于外挂策略
    assert source["DefaultAudioStreamIndex"] == AUDIO_INDEX

    # 明确关闭字幕（-1 是协议有效值）：重进仍关
    resp = sclient.post(
        "/Sessions/Playing/Progress",
        headers=headers,
        json={
            "ItemId": guid,
            "MediaSourceId": ms_id,
            "PositionTicks": 120 * 10_000_000,
            "SubtitleStreamIndex": -1,
        },
    )
    assert resp.status_code == 204
    source = _playback_info(sclient, token, guid)["MediaSources"][0]
    assert source["DefaultSubtitleStreamIndex"] == -1


def test_track_memory_ignores_dangling_index(sclient: TestClient, subtitle_env: dict) -> None:
    """悬空序号（指到不存在的流）丢弃不落库，默认轨回到选择策略。"""
    token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    resp = sclient.post(
        "/Sessions/Playing/Progress",
        headers={"X-Emby-Token": token},
        json={"ItemId": guid, "PositionTicks": 60 * 10_000_000, "SubtitleStreamIndex": 99},
    )
    assert resp.status_code == 204
    source = _playback_info(sclient, token, guid)["MediaSources"][0]
    assert source["DefaultSubtitleStreamIndex"] == EXTERNAL_INDEX  # 策略：外挂优先


def test_member_visibility_enforced_on_subtitle_route(
    sclient: TestClient, subtitle_env: dict
) -> None:
    """白名单外成员拉字幕 404（GUID 可枚举，字幕路径不能直进）。"""
    from fastapi.testclient import TestClient as _TC  # noqa: F401 -- 类型提示用

    admin_token = jf_login(sclient)
    guid = item_guid(subtitle_env["movie"])
    ms_id = _playback_info(sclient, admin_token, guid)["MediaSources"][0]["Id"]

    # 建一个只可见剧集库的成员（电影库在白名单外）
    created = sclient.post(
        "/api/v1/members", json={"username": "subuser", "password": "sub-pass-123"}
    )
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]
    updated = sclient.put(
        f"/api/v1/members/{member_id}",
        json={"all_libraries": False, "library_ids": [subtitle_env["tv_lib"]]},
    )
    assert updated.status_code == 200, updated.text
    login = sclient.post(
        "/Users/AuthenticateByName",
        json={"Username": "subuser", "Pw": "sub-pass-123"},
        headers={
            "Authorization": (
                'MediaBrowser Client="Infuse", Device="iPad", '
                'DeviceId="sub-device-1", Version="8.2"'
            )
        },
    )
    assert login.status_code == 200, login.text
    member_token = login.json()["AccessToken"]

    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/0/Stream.srt",
        params={"ApiKey": member_token},
    )
    assert resp.status_code == 404
    # 超管照常可取
    resp = sclient.get(
        f"/Videos/{guid}/{ms_id}/Subtitles/{EXTERNAL_INDEX}/0/Stream.srt",
        params={"ApiKey": admin_token},
    )
    assert resp.status_code == 200
