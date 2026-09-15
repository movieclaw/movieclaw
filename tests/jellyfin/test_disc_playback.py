"""原盘（BDMV）在 Jellyfin 兼容层的播放形态（docs/design/disc-playback.md §3.3/§3.5）。

- 单剪辑主片：MediaSource 伪装成 m2ts 文件，/Videos/{id}/stream(.m2ts) 按 Range 直出；
- 多剪辑主片：PlaybackInfo 给 TranscodingUrl，master.m3u8 起 copy remux 会话；
- 原盘不可下载；台账无清单且不许读盘时浏览态保持原样。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jellyfin.helpers import AUTH_HEADER, jf_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.bluray import disc_playlist_record, read_main_playlist
from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.session import get_session_manager, reset_session_manager
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, LibraryFile, MediaItem
from movieclaw_jellyfin.ids import item_guid, library_guid, media_source_guid

FAKE_FFMPEG = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n#EXT-X-MAP:URI=\\"init.mp4\\"\\n")
(out.parent / "init.mp4").write_bytes(b"INIT")
(out.parent / "seg00000.m4s").write_bytes(b"SEGMENT-DATA")
time.sleep(300)
"""


def _mpls(*items: tuple[str, int, int]) -> bytes:
    body = bytearray(b"\0\0" + len(items).to_bytes(2, "big") + b"\0\0")
    for clip_id, in_time, out_time in items:
        core = (
            clip_id.encode("ascii")
            + b"M2TS"
            + b"\0\0"
            + b"\0"
            + in_time.to_bytes(4, "big")
            + out_time.to_bytes(4, "big")
        )
        body += len(core).to_bytes(2, "big") + core
    header = bytearray(b"MPLS0100" + (20).to_bytes(4, "big") + b"\0" * 8)
    return bytes(header + len(body).to_bytes(4, "big") + body)


def _make_disc(root: Path, name: str, clips: list[tuple[str, int, int]]) -> Path:
    disc = root / name
    (disc / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (disc / "BDMV" / "STREAM").mkdir()
    for clip_id, _, _ in clips:
        (disc / "BDMV" / "STREAM" / f"{clip_id}.m2ts").write_bytes(b"M2TS-BYTES" * 100)
    (disc / "BDMV" / "PLAYLIST" / "00001.mpls").write_bytes(_mpls(*clips))
    return disc


@pytest.fixture
def transcode_env(tmp_path, monkeypatch):
    """假 ffmpeg + 独立转码目录：master.m3u8 用例只验 HTTP 形态。"""
    monkeypatch.setenv("MOVIECLAW_TRANSCODE_DIR", str(tmp_path / "transcodes"))
    get_settings.cache_clear()
    reset_session_manager()
    calls: list[dict] = []

    def fake_build(
        plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None, **kw
    ):
        calls.append({"source_path": source_path, "plan": plan, **kw})
        playlist = Path(session_dir) / ("live.m3u8" if start_number is not None else "index.m3u8")
        return TranscodeCommand(
            argv=["python3", "-c", FAKE_FFMPEG, str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)
    yield calls
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        get_session_manager().shutdown()
    )
    reset_session_manager()


def _seed_discs(client: TestClient, seeded: dict, media_root: Path) -> dict:
    single = _make_disc(media_root, "Single (2021)", [("00001", 45_000 * 10, 45_000 * 130)])
    multi = _make_disc(
        media_root, "Multi (2022)", [("00001", 0, 45_000 * 60), ("00002", 0, 45_000 * 90)]
    )
    legacy = _make_disc(media_root, "Legacy (2020)", [("00001", 0, 45_000 * 60)])

    async def _insert() -> dict:
        async with get_database().session() as session:
            items = {
                key: MediaItem(
                    kind="movie", tmdb_id=900 + i, title=title, original_title=title, year=2020 + i
                )
                for i, (key, title) in enumerate(
                    [("single", "单剪辑原盘"), ("multi", "多剪辑原盘"), ("legacy", "存量原盘")]
                )
            }
            session.add_all(items.values())
            await session.flush()
            rows = {}
            for key, disc, record in [
                ("single", single, disc_playlist_record(read_main_playlist(single))),
                ("multi", multi, disc_playlist_record(read_main_playlist(multi))),
                ("legacy", legacy, None),
            ]:
                row = LibraryFile(
                    library_id=seeded["movie_lib"],
                    media_item_id=items[key].id,
                    file_path=str(disc),
                    size_bytes=99_000,
                    container="bluray",
                    resolution="2160p",
                    video_codec="hevc",
                    hdr="HDR10",
                    bit_depth=10,
                    duration_seconds=120 if key == "single" else 150,
                    audio_streams=[
                        {"codec": "truehd", "channels": 8, "language": "eng", "default": True},
                        {"codec": "ac3", "channels": 6, "language": "chi", "default": False},
                    ],
                    subtitle_streams=[],
                    external_subtitles=[],
                    disc_playlist=record,
                    source=FileSource.SCANNED,
                )
                session.add(row)
                rows[key] = row
            await session.commit()
            for row in rows.values():
                await session.refresh(row)
            return {
                key: {"item": items[key].id, "file": rows[key].id, "dir": str(d)}
                for key, d in [("single", single), ("multi", multi), ("legacy", legacy)]
            }

    return client.portal.call(_insert)  # type: ignore[attr-defined]


def _auth(client: TestClient) -> dict:
    return {"ApiKey": jf_login(client)}


def test_single_clip_disc_is_presented_and_streamed_as_m2ts(
    client: TestClient, seeded: dict, media_root: Path
) -> None:
    discs = _seed_discs(client, seeded, media_root)
    guid = item_guid(discs["single"]["item"])
    auth = _auth(client)

    item = client.get(f"/Items/{guid}", params=auth).json()
    assert item["Container"] == "m2ts"
    assert item["CanDownload"] is False
    source = item["MediaSources"][0]
    assert source["Path"].endswith("/BDMV/STREAM/00001.m2ts")
    assert source["Container"] == "m2ts"
    assert source["SupportsDirectPlay"] is True

    info = client.post(f"/Items/{guid}/PlaybackInfo", params=auth).json()
    ms = info["MediaSources"][0]
    assert ms["Container"] == "m2ts" and "TranscodingUrl" not in ms

    # Infuse 按 Container 拼 stream.m2ts；VidHub 直接打 /stream：都要 206 + video/mp2t
    for suffix in (".m2ts", ""):
        resp = client.get(
            f"/Videos/{guid}/stream{suffix}",
            params={**auth, "static": "true"},
            headers={"Range": "bytes=0-9", "Authorization": AUTH_HEADER},
        )
        assert resp.status_code == 206, resp.text
        assert resp.headers["content-type"].startswith("video/mp2t")
        assert resp.content == b"M2TS-BYTES"

    # 原盘不可下载
    assert client.get(f"/Items/{guid}/Download", params=auth).status_code == 404


def test_multi_clip_disc_advertises_transcoding_url_and_serves_master(
    client: TestClient, seeded: dict, media_root: Path, transcode_env: list
) -> None:
    discs = _seed_discs(client, seeded, media_root)
    guid = item_guid(discs["multi"]["item"])
    ms_guid = media_source_guid(discs["multi"]["file"])
    auth = _auth(client)

    item = client.get(f"/Items/{guid}", params=auth).json()
    assert "Container" not in item
    assert item["MediaSources"][0]["SupportsDirectPlay"] is False

    # 客户端指定第二条音轨（协议编号 = 外挂数 0 + 1 + k）
    info = client.post(
        f"/Items/{guid}/PlaybackInfo", params=auth, json={"AudioStreamIndex": 2}
    ).json()
    ms = info["MediaSources"][0]
    assert ms["SupportsDirectPlay"] is False and ms["SupportsDirectStream"] is False
    assert ms["SupportsTranscoding"] is True
    assert ms["TranscodingSubProtocol"] == "hls" and ms["TranscodingContainer"] == "mp4"
    url = ms["TranscodingUrl"]
    assert url.startswith(f"/Videos/{guid}/master.m3u8?")
    assert f"MediaSourceId={ms_guid}" in url
    assert f"PlaySessionId={info['PlaySessionId']}" in url
    assert "AudioStreamIndex=2" in url
    assert "ApiKey=" in url

    # 直接按 stream 取多剪辑 → 404（没有单文件）
    assert (
        client.get(f"/Videos/{guid}/stream", params={**auth, "static": "true"}).status_code == 404
    )

    master = client.get(url, headers={"Authorization": AUTH_HEADER})
    assert master.status_code == 200, master.text
    assert master.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    lines = master.text.splitlines()
    assert lines[0] == "#EXTM3U"
    media_line = next(line for line in lines if line.startswith("/api/v1/playback/sessions/"))
    assert media_line.endswith("index.m3u8?token=" + media_line.split("token=")[1])

    # 会话按 concat 清单起，音轨是客户端选的第二条（AC-3），视频与音频都 copy
    assert transcode_env, "应当已起一个 ffmpeg 会话"
    call = transcode_env[-1]
    assert call["source_path"].endswith("/source.concat")
    assert call.get("input_format") == "concat"
    plan = call["plan"]
    assert plan.video.action == "copy" and plan.audio.action == "copy"
    assert plan.audio.track_ref == "embedded:1"
    concat_text = Path(call["source_path"]).read_text(encoding="utf-8")
    assert concat_text.startswith("ffconcat version 1.0\n")
    assert concat_text.count("\nfile '") == 2

    # 媒体列表能拉到（网页播放器会话端点，带 token）
    playlist = client.get(media_line)
    assert playlist.status_code == 200 and playlist.text.startswith("#EXTM3U")

    # 不指定音轨：默认轨是 TrueHD，装不进 fMP4，自动回退到 AC-3 核心（第二条）
    fallback = client.get(
        f"/Videos/{guid}/master.m3u8",
        params={**auth, "MediaSourceId": ms_guid},
        headers={"Authorization": AUTH_HEADER},
    )
    assert fallback.status_code == 200, fallback.text
    assert transcode_env[-1]["plan"].audio.track_ref == "embedded:1"
    assert transcode_env[-1]["plan"].audio.codec == "ac3"
    media_line = next(
        line for line in fallback.text.splitlines() if line.startswith("/api/v1/playback/sessions/")
    )

    # 播放器上报 Stopped → 这台设备的会话被收掉
    session_id = media_line.split("/sessions/")[1].split("/")[0]
    assert get_session_manager().get(session_id, member_id=0) is not None
    resp = client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": guid, "PositionTicks": 0},
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 204
    assert get_session_manager().get(session_id, member_id=0) is None


def test_legacy_disc_row_without_ledger_record_reads_disc_at_playback_time(
    client: TestClient, seeded: dict, media_root: Path
) -> None:
    discs = _seed_discs(client, seeded, media_root)
    guid = item_guid(discs["legacy"]["item"])
    auth = _auth(client)
    # 浏览态不读盘：保持台账原样（容器仍是 bluray）
    item = client.get(f"/Items/{guid}", params=auth).json()
    assert item["Container"] == "bluray"
    # 播放协商允许读盘：解析出单剪辑
    info = client.post(f"/Items/{guid}/PlaybackInfo", params=auth).json()
    assert info["MediaSources"][0]["Container"] == "m2ts"
    resp = client.get(
        f"/Videos/{guid}/stream",
        params={**auth, "static": "true"},
        headers={"Range": "bytes=0-3", "Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 206


def test_disc_rows_render_in_list_paths(client: TestClient, seeded: dict, media_root: Path) -> None:
    """列表路径（Items/Latest、Items）也要能渲染原盘行。

    列表 DTO 在 session 关闭后才构建，文件行只装 ``_list_load_columns``
    白名单里的列；``_apply_leaf_media_fields`` 对每个原盘叶子都要读
    ``disc_playlist``，漏进白名单就是一次惰性加载 → DetachedInstanceError，
    整条「最近添加」500。
    """
    discs = _seed_discs(client, seeded, media_root)
    auth = _auth(client)

    latest = client.get("/Items/Latest", params={**auth, "limit": 20})
    assert latest.status_code == 200, latest.text
    by_id = {i["Id"]: i for i in latest.json()}

    single = by_id[item_guid(discs["single"]["item"])]
    assert single["Container"] == "m2ts"
    # 多剪辑没有单文件容器可报；存量行无台账清单、浏览态不读盘，保持 bluray
    assert "Container" not in by_id[item_guid(discs["multi"]["item"])]
    assert by_id[item_guid(discs["legacy"]["item"])]["Container"] == "bluray"

    items = client.get(
        "/Items",
        params={**auth, "parentId": library_guid(seeded["movie_lib"]), "recursive": "true"},
    )
    assert items.status_code == 200, items.text
    rows = {i["Id"]: i for i in items.json()["Items"]}
    assert rows[item_guid(discs["single"]["item"])]["Container"] == "m2ts"
