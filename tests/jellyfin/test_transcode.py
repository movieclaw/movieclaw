"""Jellyfin 兼容层的转码协商（docs/design/jellyfin-transcode.md）。

- 协商解析与 TranscodingUrl 契约是纯函数，直接单测；
- 全链路走真 HTTP：PlaybackInfo → master.m3u8 → 会话端点 → 停播/心跳，
  ffmpeg 用假进程替身（只验协议形态与会话生命周期，不验编码）。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
from fastapi.testclient import TestClient

from jellyfin.helpers import AUTH_HEADER, jf_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.session import get_session_manager, reset_session_manager
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, LibraryFile, MediaItem
from movieclaw_jellyfin import transcode
from movieclaw_jellyfin.ids import item_guid, media_source_guid
from movieclaw_playback.decide import PlaybackTier

FAKE_FFMPEG = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
(out.parent / "init.mp4").write_bytes(b"INIT")
(out.parent / "seg00000.m4s").write_bytes(b"SEGMENT-DATA")
(out.parent / "seg00000.ts").write_bytes(b"TS-SEGMENT-DATA")
# 列表登记首个分片（fMP4 与 TS 两种名字都写：会话管理器按列表台账判分片完成）
out.write_text(
    "#EXTM3U\\n#EXT-X-VERSION:7\\n#EXT-X-MAP:URI=\\"init.mp4\\"\\n"
    "#EXTINF:4.000000,\\nseg00000.m4s\\n#EXTINF:4.000000,\\nseg00000.ts\\n"
)
time.sleep(300)
"""

# ---------------------------------------------------------------------------
# 纯函数：协商解析、直连判定、URL 契约
# ---------------------------------------------------------------------------


def test_parse_negotiation_prefers_query_then_body_then_device_profile():
    body = {
        "MaxStreamingBitrate": 5_000_000,
        "DeviceProfile": {"MaxStreamingBitrate": 3_000_000},
        "EnableDirectPlay": "false",
        "StartTimeTicks": "600000000",
        "AudioStreamIndex": "2",
    }
    n = transcode.parse_negotiation({"maxStreamingBitrate": "8000000"}, body)
    assert n.max_bitrate_bps == 8_000_000
    assert n.enable_direct_play is False and n.wants_transcode
    assert n.start_ms == 60_000
    assert n.audio_stream_index == 2

    only_profile = transcode.parse_negotiation({}, {"DeviceProfile": {"MaxStreamingBitrate": 3e6}})
    assert only_profile.max_bitrate_bps == 3_000_000
    assert only_profile.enable_direct_play is True and not only_profile.wants_transcode

    # 0 / 负数 = 不限；空 body 与坏值都不炸
    assert transcode.parse_negotiation({"MaxStreamingBitrate": "0"}, None).max_bitrate_bps is None
    assert transcode.parse_negotiation({"MaxStreamingBitrate": "abc"}, {}).max_bitrate_bps is None


def test_direct_play_allowed_follows_stream_builder_bitrate_rule():
    limited = transcode.Negotiation(max_bitrate_bps=4_000_000)
    assert transcode.direct_play_allowed(3_000_000, limited)
    assert not transcode.direct_play_allowed(25_000_000, limited)
    assert transcode.direct_play_allowed(None, limited), "源码率未知按允许"
    assert transcode.direct_play_allowed(25_000_000, transcode.Negotiation())
    forced = transcode.Negotiation(enable_direct_play=False)
    assert not transcode.direct_play_allowed(1, forced)
    no_copy = transcode.Negotiation(allow_video_stream_copy=False)
    assert not transcode.direct_play_allowed(1, no_copy)


def test_transcoding_url_roundtrip():
    url = transcode.build_transcoding_url(
        "item",
        media_source_id="ms",
        play_session_id="psid",
        token="tok",
        video_bitrate_bps=1_500_000,
        max_height=480,
        audio_stream_index=1,
        start_ms=90_000,
        bitrate_exceeded=True,
    )
    assert url.startswith("/Videos/item/master.m3u8?")
    assert "TranscodeReasons=ContainerBitrateExceedsLimit" in url
    assert "ApiKey=tok" in url and "VideoCodec=h264" in url
    assert "SegmentContainer=mp4" in url and "MaxAudioChannels" not in url
    query = dict(parse_qsl(url.split("?", 1)[1]))
    params = transcode.parse_transcode_params(query)
    assert params == transcode.TranscodeParams(
        media_source_id="ms",
        play_session_id="psid",
        video_bitrate_bps=1_500_000,
        max_height=480,
        audio_stream_index=1,
        start_ms=90_000,
    )
    # 播放器申报了容器与音频能力（Infuse）：原样进 URL、master 端原样还原
    infuse = transcode.build_transcoding_url(
        "item",
        media_source_id="ms",
        play_session_id="psid",
        token="tok",
        video_bitrate_bps=None,
        max_height=None,
        audio_stream_index=None,
        start_ms=None,
        bitrate_exceeded=False,
        segment_container="ts",
        audio_codecs=("aac",),
        max_audio_channels=2,
    )
    assert "SegmentContainer=ts" in infuse and "AudioCodec=aac&" in infuse
    assert "MaxAudioChannels=2" in infuse
    restored = transcode.parse_transcode_params(dict(parse_qsl(infuse.split("?", 1)[1])))
    assert restored.segment_container == "ts"
    assert restored.audio_codecs == ("aac",) and restored.max_audio_channels == 2
    bare = transcode.build_transcoding_url(
        "item",
        media_source_id="ms",
        play_session_id="psid",
        token="tok",
        video_bitrate_bps=None,
        max_height=None,
        audio_stream_index=None,
        start_ms=None,
        bitrate_exceeded=False,
    )
    assert "VideoBitrate" not in bare and "TranscodeReasons" not in bare
    assert transcode.parse_transcode_params({}) == transcode.TranscodeParams()


def test_play_session_registry_drops_dead_entries():
    transcode.reset_play_sessions()
    transcode.register_play_session("p1", "s1", alive=lambda _sid: True)
    transcode.register_play_session("p2", "s2", alive=lambda sid: sid != "s1")
    assert transcode.take_play_session("p1") is None, "登记新条目时清掉已死会话"
    assert transcode.take_play_session("p2") == "s2"
    assert transcode.take_play_session("p2") is None, "取出即注销"
    assert transcode.take_play_session(None) is None


# ---------------------------------------------------------------------------
# HTTP 全链路（假 ffmpeg）
# ---------------------------------------------------------------------------


@pytest.fixture
def transcode_env(client, tmp_path, monkeypatch):
    """假 ffmpeg + 独立转码目录 + 无显卡。

    依赖 ``client``：会话里的假 ffmpeg 挂在应用事件循环上，收尾要在应用还
    活着时经 portal 在同一个循环里 shutdown（测试可以把会话留着不停）。
    """
    monkeypatch.setenv("MOVIECLAW_TRANSCODE_DIR", str(tmp_path / "transcodes"))
    get_settings.cache_clear()
    reset_session_manager()
    transcode.reset_play_sessions()
    monkeypatch.setattr(playback_plan, "hardware_available", lambda: False)
    calls: list[dict] = []

    def fake_build(
        plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None, **kw
    ):
        calls.append({"source_path": source_path, "plan": plan, "start_ms": start_ms, **kw})
        playlist = Path(session_dir) / ("live.m3u8" if start_number is not None else "index.m3u8")
        return TranscodeCommand(
            argv=["python3", "-c", FAKE_FFMPEG, str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)
    yield calls
    client.portal.call(get_session_manager().shutdown)  # type: ignore[attr-defined]
    reset_session_manager()
    transcode.reset_play_sessions()


def _enable_software_transcode(client: TestClient) -> None:
    client.portal.call(  # type: ignore[attr-defined]
        get_setting_store().set, PlaybackPolicySetting(software_transcode_enabled=True)
    )


def _seed_sdr_movie(client: TestClient, seeded: dict, media_root: Path) -> dict:
    """1080p H.264 SDR，8 Mbps；DTS 5.1 默认轨 + AAC 立体声；内封 SRT + PGS。"""
    path = media_root / "Heat (1995)" / "Heat.1995.1080p.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"H" * 4096)

    async def _insert() -> dict:
        async with get_database().session() as session:
            item = MediaItem(
                kind="movie", tmdb_id=949, title="盗火线", original_title="Heat", year=1995
            )
            session.add(item)
            await session.flush()
            row = LibraryFile(
                library_id=seeded["movie_lib"],
                media_item_id=item.id,
                file_path=str(path),
                size_bytes=4096,
                container="mkv",
                resolution="1080p",
                video_codec="h264",
                duration_seconds=170 * 60,
                bit_rate=8_000_000,
                audio_streams=[
                    {"codec": "dts", "channels": 6, "language": "eng", "default": True},
                    {"codec": "aac", "channels": 2, "language": "chi", "default": False},
                ],
                subtitle_streams=[
                    {"codec": "subrip", "language": "chi", "default": False, "forced": False},
                    {"codec": "hdmv_pgs_subtitle", "language": "eng", "default": False},
                ],
                external_subtitles=[],
                source=FileSource.SCANNED,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return {"item": item.id, "file": row.id}

    return client.portal.call(_insert)  # type: ignore[attr-defined]


def _auth(client: TestClient) -> dict:
    return {"ApiKey": jf_login(client)}


def _media_line(master_text: str) -> str:
    """master 里的媒体列表地址：必须是同目录相对路径 ``main.m3u8?session=…&token=…``。

    Infuse 把「master 所在目录 + 地址串」直接拼接（不按 RFC 3986 解析），绝对路径
    会被拼成 ``/Videos/{item}//api/…`` 而 404（2026-09-23 真机）。
    """
    line = next(line for line in master_text.splitlines() if not line.startswith("#"))
    assert line.startswith("main.m3u8?session=") and "&token=" in line, line
    assert "&ApiKey=" in line, "每条 HLS 地址自带设备凭据（播放器媒体内核不带认证头）"
    return line


def _session_id(media_line: str) -> str:
    return media_line.split("session=")[1].split("&")[0]


def test_playback_info_stays_direct_when_bitrate_fits(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """线路装得下：直连不变，只多声明 SupportsTranscoding=true（真 Jellyfin 同款形态）。"""
    _enable_software_transcode(client)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)

    plain = client.post(f"/Items/{guid}/PlaybackInfo", params=auth).json()["MediaSources"][0]
    assert plain["SupportsDirectPlay"] is True and plain["SupportsTranscoding"] is True
    assert "TranscodingUrl" not in plain
    # 位图轨在直连时保留：播放器自行解封装
    assert any(s["Type"] == "Subtitle" and s.get("Codec") == "hdmv_pgs_subtitle"
               for s in plain["MediaStreams"])

    fits = client.post(
        f"/Items/{guid}/PlaybackInfo", params={**auth, "MaxStreamingBitrate": 20_000_000}
    ).json()["MediaSources"][0]
    assert fits["SupportsDirectPlay"] is True and "TranscodingUrl" not in fits


def test_bitrate_limit_negotiates_transcode_and_starts_session(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """Infuse 形态：DeviceProfile.MaxStreamingBitrate=3 Mbps 装不下 8 Mbps 的源。

    协商：不可直连 + TranscodingUrl（高度/码率按与网页端同一条规则：3 Mbps
    × 0.8 装不下 1080p 阶梯的七五折 → 降到 480p、限 1.5 Mbps）；内封 SRT 改为
    旁挂投递、PGS 从流列表撤掉；DTS 默认轨要转 E-AC-3。
    master.m3u8：按 URL 起软转会话，媒体列表与分片都挂在 /Videos/{item}/ 下
    （Jellyfin 形态的相对地址），内容与网页播放器的会话端点同源。
    """
    _enable_software_transcode(client)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    ms_guid = media_source_guid(movie["file"])
    auth = _auth(client)

    info = client.post(
        f"/Items/{guid}/PlaybackInfo",
        params=auth,
        json={"DeviceProfile": {"MaxStreamingBitrate": 3_000_000}, "StartTimeTicks": 600_000_000},
    ).json()
    ms = info["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is False and ms["SupportsDirectStream"] is False
    assert ms["SupportsTranscoding"] is True
    assert ms["TranscodingContainer"] == "mp4" and ms["TranscodingSubProtocol"] == "hls"
    url = ms["TranscodingUrl"]
    assert url.startswith(f"/Videos/{guid}/master.m3u8?")
    assert f"MediaSourceId={ms_guid}" in url
    assert f"PlaySessionId={info['PlaySessionId']}" in url
    assert "VideoBitrate=1500000" in url and "MaxHeight=480" in url
    assert "AudioStreamIndex=1" in url and "StartTimeTicks=600000000" in url
    assert "TranscodeReasons=ContainerBitrateExceedsLimit" in url
    assert ms["DefaultAudioStreamIndex"] == 1
    subs = [s for s in ms["MediaStreams"] if s["Type"] == "Subtitle"]
    assert [s["Codec"] for s in subs] == ["subrip"], "PGS 在转码时撤掉"
    assert subs[0]["DeliveryMethod"] == "External"
    assert subs[0]["DeliveryUrl"].startswith(f"/Videos/{guid}/{ms_guid}/Subtitles/3/0/Stream.srt")

    master = client.get(url, headers={"Authorization": AUTH_HEADER})
    assert master.status_code == 200, master.text
    assert master.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    media_line = _media_line(master.text)

    assert transcode_env, "应当已起一个 ffmpeg 会话"
    call = transcode_env[-1]
    plan = call["plan"]
    assert plan.tier is PlaybackTier.SOFTWARE_TRANSCODE
    assert plan.video.action == "transcode" and plan.video.codec == "h264"
    assert plan.video.height == 480 and plan.video.bitrate_cap_bps == 1_500_000
    assert plan.audio.action == "transcode" and plan.audio.codec == "eac3"
    assert plan.audio.track_ref == "embedded:0"
    assert call["source_path"].endswith("Heat.1995.1080p.mkv")
    # 起播位置：StartTimeTicks 60 秒 → 对齐到 4 秒分片边界
    assert call["start_ms"] == 60_000

    # 媒体列表按 Infuse 的拼接方式取：master 目录 + 相对地址
    playlist = client.get(f"/Videos/{guid}/{media_line}")
    assert playlist.status_code == 200, playlist.text
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist.text
    session_id = _session_id(media_line)
    query = f"?session={session_id}&token="
    assert f'#EXT-X-MAP:URI="hls1/main/init.mp4{query}' in playlist.text
    segment_line = next(
        line for line in playlist.text.splitlines() if line.startswith("hls1/main/seg")
    )
    assert segment_line.startswith(f"hls1/main/seg00000.m4s{query}")
    # 分片同样从 /Videos/{item}/ 目录下拼出来；初始化段由假 ffmpeg 写好
    segment_query = "?" + segment_line.split("?", 1)[1]
    init = client.get(f"/Videos/{guid}/hls1/main/init.mp4{segment_query}")
    assert init.status_code == 200 and init.content == b"INIT"
    # 文件名不在白名单 / token 不对 → Jellyfin 形态的空 404
    bad_name = client.get(f"/Videos/{guid}/hls1/main/..%2Fetc%2Fpasswd{segment_query}")
    assert bad_name.status_code == 404
    bad_token = client.get(
        f"/Videos/{guid}/main.m3u8", params={**auth, "session": session_id, "token": "nope"}
    )
    assert bad_token.status_code == 404
    # 没有设备凭据：与本路由组其他端点一样 401
    no_device = client.get(f"/Videos/{guid}/main.m3u8?session={session_id}&token=x")
    assert no_device.status_code == 401

    # 播放器上报 Stopped（带 PlaySessionId）→ 精确停掉这个会话
    manager = get_session_manager()
    assert manager.get(session_id, member_id=0) is not None
    resp = client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": guid, "PositionTicks": 0, "PlaySessionId": info["PlaySessionId"]},
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 204
    assert manager.get(session_id, member_id=0) is None


#: Infuse 8.5 发来的 DeviceProfile 转码档（2026-09-23 NAS 抓包原样，去掉无关键）：
#: 视频只接受 HLS + MPEG-TS 容器、音频只要 AAC 双声道。
INFUSE_TRANSCODING_PROFILES = [
    {
        "Type": "Audio",
        "Container": "aac",
        "Protocol": "hls",
        "AudioCodec": "aac",
        "MaxAudioChannels": "2",
        "Context": "Streaming",
    },
    {
        "Type": "Video",
        "Container": "ts",
        "Protocol": "hls",
        "VideoCodec": "hevc,h264,av1",
        "AudioCodec": "aac",
        "MaxAudioChannels": "2",
        "ManifestSubtitles": "vtt",
        "Context": "Streaming",
    },
]


def test_infuse_profile_gets_mpegts_segments_and_aac_stereo(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """Infuse 形态的完整申报：TranscodingProfiles 说只认 ts + aac 双声道。

    协商：TranscodingContainer=ts，URL 带 SegmentContainer=ts / AudioCodec=aac /
    MaxAudioChannels=2；master 起会话时计划容器 hls-ts、DTS 5.1 默认轨转 AAC 立体声；
    媒体列表没有 EXT-X-MAP，分片是 .ts 且以 video/mp2t 交付——2026-09-23 真机：
    Infuse 拿到 fMP4 分片用 Range 探一下就报错，连 init.mp4 都不取。
    """
    _enable_software_transcode(client)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)

    info = client.post(
        f"/Items/{guid}/PlaybackInfo",
        params=auth,
        json={
            "DeviceProfile": {
                "MaxStreamingBitrate": 3_000_000,
                "TranscodingProfiles": INFUSE_TRANSCODING_PROFILES,
            }
        },
    ).json()
    ms = info["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is False and ms["SupportsTranscoding"] is True
    assert ms["TranscodingContainer"] == "ts" and ms["TranscodingSubProtocol"] == "hls"
    url = ms["TranscodingUrl"]
    assert "SegmentContainer=ts" in url and "AudioCodec=aac&" in url
    assert "MaxAudioChannels=2" in url

    master = client.get(url, headers={"Authorization": AUTH_HEADER})
    assert master.status_code == 200, master.text
    media_line = _media_line(master.text)
    plan = transcode_env[-1]["plan"]
    assert plan.container == "hls-ts"
    assert plan.audio.action == "transcode" and plan.audio.codec == "aac"
    assert plan.audio.channels == 2 and plan.audio.downmix is True

    playlist = client.get(f"/Videos/{guid}/{media_line}")
    assert playlist.status_code == 200, playlist.text
    assert "#EXT-X-MAP" not in playlist.text, "TS 分片自含 PAT/PMT，没有 init 段"
    segment_line = next(
        line for line in playlist.text.splitlines() if line.startswith("hls1/main/seg")
    )
    assert segment_line.startswith("hls1/main/seg00000.ts?session=")
    segment = client.get(f"/Videos/{guid}/{segment_line}", headers={"Range": "bytes=0-"})
    assert segment.status_code in (200, 206), segment.text
    assert segment.headers["content-type"].startswith("video/mp2t")
    assert segment.content == b"TS-SEGMENT-DATA"


def _only_remote_worker(monkeypatch, *, ts_capable: bool) -> None:
    """NAS 本机没有硬件、只连着一台 Mac Worker；旧版（ts_capable=False）只会回传 fMP4。"""
    from movieclaw_jellyfin.routes import playback as jf_playback

    monkeypatch.setattr(playback_plan, "hardware_available", lambda: True)
    monkeypatch.setattr(jf_playback, "available_local_backends", lambda: ())
    monkeypatch.setattr(
        jf_playback,
        "remote_worker_available",
        lambda _backend="videotoolbox", segment_type="fmp4": (
            ts_capable or segment_type == "fmp4"
        ),
    )


def test_infuse_ts_never_goes_to_outdated_remote_worker(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list, monkeypatch, caplog
) -> None:
    """issue #444：旧版 Mac Worker 的上传代理只放行 fMP4，TS 分片在它本机被悄悄拒收，
    ffmpeg 照样退出码 0，播放器等满 30 秒拿 404。硬件只剩这种 Worker 时，Infuse 的
    TS 转码按「无硬件」协商：软转没开就直连应答、日志点名更新 Worker；软转开着就退软转。"""
    _only_remote_worker(monkeypatch, ts_capable=False)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)
    body = {
        "DeviceProfile": {
            "MaxStreamingBitrate": 3_000_000,
            "TranscodingProfiles": INFUSE_TRANSCODING_PROFILES,
        }
    }

    with caplog.at_level("WARNING"):
        info = client.post(f"/Items/{guid}/PlaybackInfo", params=auth, json=body).json()
    ms = info["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is True and "TranscodingUrl" not in ms
    assert any("MovieClaw 转码器" in r.message for r in caplog.records)

    _enable_software_transcode(client)
    ms = client.post(f"/Items/{guid}/PlaybackInfo", params=auth, json=body).json()
    master = client.get(
        ms["MediaSources"][0]["TranscodingUrl"], headers={"Authorization": AUTH_HEADER}
    )
    assert master.status_code == 200, master.text
    plan = transcode_env[-1]["plan"]
    assert plan.container == "hls-ts" and plan.tier is PlaybackTier.SOFTWARE_TRANSCODE


@pytest.mark.parametrize(
    ("segment_container", "ts_capable", "hardware"),
    [
        ("mp4", False, True),  # fMP4（网页播放器 / 未申报 TS 的客户端）：旧版照样用
        ("ts", True, True),  # 新版 Worker 声明了 mpegts
        ("ts", False, False),  # 只剩旧版 Worker：TS 按无硬件
    ],
)
def test_transcode_policy_hardware_follows_worker_segment_types(
    client: TestClient, monkeypatch, segment_container: str, ts_capable: bool, hardware: bool
) -> None:
    from movieclaw_jellyfin.routes import playback as jf_playback

    _only_remote_worker(monkeypatch, ts_capable=ts_capable)
    policy = client.portal.call(  # type: ignore[attr-defined]
        jf_playback._load_policy_for, segment_container
    )
    assert policy.hardware_available is hardware


def test_forced_transcode_without_bitrate_and_audio_selection(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """EnableDirectPlay=false（jellyfin-web 换清晰度形态）：不限码率也转码，
    高度取源 1080p、不带 VideoBitrate；点选 AAC 立体声轨则音频原样 copy。"""
    _enable_software_transcode(client)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)

    ms = client.post(
        f"/Items/{guid}/PlaybackInfo",
        params={**auth, "EnableDirectPlay": "false", "AudioStreamIndex": 2},
    ).json()["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is False
    url = ms["TranscodingUrl"]
    assert "MaxHeight=1080" in url and "VideoBitrate" not in url
    assert "TranscodeReasons" not in url and "AudioStreamIndex=2" in url

    assert client.get(url, headers={"Authorization": AUTH_HEADER}).status_code == 200
    plan = transcode_env[-1]["plan"]
    assert plan.video.height == 1080 and plan.video.bitrate_cap_bps is None
    assert plan.audio.action == "copy" and plan.audio.track_ref == "embedded:1"


def test_transcode_unavailable_falls_back_to_direct_play(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """无显卡且软转未开：协商结果与加入转码前完全一致——直连、不声明转码。"""
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)

    ms = client.post(
        f"/Items/{guid}/PlaybackInfo", params={**auth, "MaxStreamingBitrate": 3_000_000}
    ).json()["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is True and ms["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in ms
    # 直接打 master 也拒绝，不会起会话
    resp = client.get(
        f"/Videos/{guid}/master.m3u8", params=auth, headers={"Authorization": AUTH_HEADER}
    )
    assert resp.status_code == 404 and not transcode_env


def test_strm_and_hdr_without_gpu_never_transcode(
    client: TestClient, seeded: dict, transcode_env: list
) -> None:
    """预播种的电影：strm 版本永远直连；HDR10 本地版本无显卡时拒绝软件 tone-map，
    同样回落直连。"""
    _enable_software_transcode(client)
    guid = item_guid(seeded["movie"])
    auth = _auth(client)
    sources = client.post(
        f"/Items/{guid}/PlaybackInfo", params={**auth, "MaxStreamingBitrate": 1_000_000}
    ).json()["MediaSources"]
    for source in sources:
        assert source["SupportsDirectPlay"] is True, source["Protocol"]
        assert source["SupportsTranscoding"] is False
        assert "TranscodingUrl" not in source


def test_progress_keeps_session_alive_and_active_encodings_stops_it(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    """Progress / Ping 给转码会话续命；DELETE /Videos/ActiveEncodings 按
    playSessionId 精确停，缺参数时按设备停。"""
    _enable_software_transcode(client)
    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    auth = _auth(client)
    manager = get_session_manager()

    def _start() -> tuple[str, str]:
        info = client.post(
            f"/Items/{guid}/PlaybackInfo", params={**auth, "MaxStreamingBitrate": 3_000_000}
        ).json()
        master = client.get(
            info["MediaSources"][0]["TranscodingUrl"], headers={"Authorization": AUTH_HEADER}
        )
        assert master.status_code == 200, master.text
        session_id = _session_id(_media_line(master.text))
        return info["PlaySessionId"], session_id

    def _stop(**params) -> None:
        resp = client.delete("/Videos/ActiveEncodings", params={**auth, **params})
        assert resp.status_code == 204

    psid, sid = _start()
    session = manager.get(sid, member_id=0)
    assert session is not None
    session.last_ping = time.monotonic() - 1000
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": guid, "PositionTicks": 100_000_000, "IsPaused": True},
        headers={"Authorization": AUTH_HEADER},
    )
    assert time.monotonic() - session.last_ping < 5, "Progress 续命"
    session.last_ping = time.monotonic() - 1000
    client.post("/Sessions/Playing/Ping", params={**auth, "playSessionId": psid})
    assert time.monotonic() - session.last_ping < 5, "Ping 续命"

    # 换清晰度：新会话起来后旧 PlaySessionId 的清理不能误杀新会话
    psid2, sid2 = _start()
    assert manager.get(sid, member_id=0) is None, "同文件旧会话在开新会话时已收掉"
    _stop(playSessionId=psid)
    assert manager.get(sid2, member_id=0) is not None, "旧 PlaySessionId 不影响新会话"
    _stop(playSessionId=psid2)
    assert manager.get(sid2, member_id=0) is None

    # 不带 playSessionId：按设备停
    _, sid3 = _start()
    _stop(deviceId="test-device-1")
    assert manager.get(sid3, member_id=0) is None


def test_embedded_text_subtitle_is_served_via_extraction(
    client: TestClient, seeded: dict, media_root: Path, monkeypatch
) -> None:
    """转码时内封文本轨按 DeliveryUrl 抽出旁挂（抽取本身用替身，内容经字幕服务原样/转格式输出）。"""
    from movieclaw_jellyfin.routes import playback as jf_playback
    from movieclaw_playback.subtitles import SubtitleRef

    movie = _seed_sdr_movie(client, seeded, media_root)
    guid = item_guid(movie["item"])
    ms_guid = media_source_guid(movie["file"])
    auth = _auth(client)
    srt = media_root / "extracted.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    seen: list[int] = []

    async def fake_extract(file, index):
        seen.append(index)
        return SubtitleRef(path=srt, format="srt")

    monkeypatch.setattr(jf_playback, "extract_embedded_subtitle_async", fake_extract)
    # 协议编号 3 = 0 外挂 + 1 视频 + 2 音轨 → 第 0 条内封轨
    resp = client.get(f"/Videos/{guid}/{ms_guid}/Subtitles/3/0/Stream.vtt", params=auth)
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith("WEBVTT") and "你好" in resp.text
    assert seen == [0]

    # 位图轨（PGS）抽不成文本 → 404
    async def fake_sup(file, index):
        return SubtitleRef(path=srt, format="sup")

    monkeypatch.setattr(jf_playback, "extract_embedded_subtitle_async", fake_sup)
    pgs = client.get(f"/Videos/{guid}/{ms_guid}/Subtitles/4/0/Stream.srt", params=auth)
    assert pgs.status_code == 404
