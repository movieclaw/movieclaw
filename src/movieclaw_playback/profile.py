"""台账行 → 决策输入的适配（docs/design/web-player.md §3.2）。

单独成模块而不是并进 ``decide``：决策引擎必须保持零 IO、零框架依赖，才能被
表驱动单测在 ``pytest -m "not integration"`` 里完整覆盖。适配器要碰
``LibraryFile`` 与 strm 判定（后者所在的 ``streaming`` 会拉进 FastAPI），
放在这里就不会污染纯函数层的 import 链。

规格全部来自 ffprobe 落库的真值（``media_probe``），不从文件名猜——库存画质
的真相来自文件本体，这是媒体库的既定原则。
"""

from __future__ import annotations

from movieclaw_db.models import LibraryFile
from movieclaw_playback.decide import AudioTrack, MediaProfile, SubtitleTrack
from movieclaw_playback.streaming import is_strm
from movieclaw_playback.subtitles import embedded_track, external_track


def media_profile_from_file(
    file: LibraryFile, *, keyframe_interval_s: float | None = None
) -> MediaProfile:
    """把一行台账装配成决策输入。

    ``keyframe_interval_s`` 由调用方从关键帧索引算出（§3.5）；未就绪时传 None，
    决策引擎会保守地不走 remux——档 1/2 的分片只能切在源片已有的 IDR 上，
    索引未知就赌不起。
    """
    return MediaProfile(
        file_id=file.id or 0,
        container=file.container,
        video_codec=file.video_codec,
        resolution=file.resolution,
        hdr=file.hdr,
        bit_depth=file.bit_depth,
        duration_ms=(file.duration_seconds * 1000) if file.duration_seconds else None,
        audio_tracks=_audio_tracks(file),
        subtitle_tracks=_subtitle_tracks(file),
        keyframe_interval_s=keyframe_interval_s,
        is_strm=is_strm(file.file_path),
    )


def _audio_tracks(file: LibraryFile) -> tuple[AudioTrack, ...]:
    return tuple(
        AudioTrack(
            ref=embedded_track(index),
            codec=(raw.get("codec") or None),
            channels=raw.get("channels"),
            language=raw.get("language"),
            is_default=bool(raw.get("default")),
        )
        for index, raw in enumerate(file.audio_streams or [])
        if isinstance(raw, dict)
    )


def _subtitle_tracks(file: LibraryFile) -> tuple[SubtitleTrack, ...]:
    """内封轨在前、外挂轨在后——与既有中性引用的编号口径保持一致。"""
    embedded = [
        SubtitleTrack(
            ref=embedded_track(index),
            codec=(raw.get("codec") or None),
            language=raw.get("language"),
            is_default=bool(raw.get("default")),
        )
        for index, raw in enumerate(file.subtitle_streams or [])
        if isinstance(raw, dict)
    ]
    external = [
        SubtitleTrack(
            ref=external_track(str(entry.get("filename"))),
            # 外挂字幕的台账字段是 format（srt/ass/…），语义等同内封的 codec
            codec=(entry.get("format") or None),
            language=entry.get("language"),
            is_external=True,
        )
        for entry in (file.external_subtitles or [])
        if isinstance(entry, dict) and entry.get("filename")
    ]
    return tuple(embedded + external)
