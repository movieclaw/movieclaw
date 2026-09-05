"""媒体库详情页字幕预览：轨引用校验、解码解析与图形字幕边界。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from movieclaw_api.api.routes.libraries import preview_file_subtitle
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.subtitle_gen import extract
from movieclaw_db.models import LibraryFile


def _file(tmp_path, *, embedded: list[dict] | None, external: list[dict] | None) -> LibraryFile:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    return LibraryFile(
        id=7,
        library_id=3,
        media_item_id=9,
        file_path=str(video),
        subtitle_streams=embedded,
        external_subtitles=external,
        source="scanned",
    )


def _session_for(file: LibraryFile) -> AsyncMock:
    session = AsyncMock()
    session.get.return_value = file
    # 路由先过可见范围校验（library-access.md）：它整取全部库的 (id, access_mode,
    # admin_visible)。给出文件所属库、超管可见，校验放行；`all()` 是同步方法，
    # 不能让 AsyncMock 把它也变成协程
    session.execute.return_value = MagicMock(all=lambda: [(file.library_id, "everyone", True)])
    return session


async def test_preview_external_subtitle_decodes_and_returns_timeline(tmp_path) -> None:
    subtitle = tmp_path / "Movie.chs.srt"
    subtitle.write_bytes(
        (
            "1\n00:00:01,250 --> 00:00:03,000\n中文字幕\nChinese subtitle\n\n"
            "2\n00:01:02,000 --> 00:01:04,500\n第二句对白\n"
        ).encode("gbk")
    )
    file = _file(
        tmp_path,
        embedded=[],
        external=[{"filename": subtitle.name, "format": "srt", "language": "chi"}],
    )

    response = await preview_file_subtitle(
        file_id=7,
        track=f"external:{subtitle.name}",
        principal=Principal(kind="admin", name="tester"),
        session=_session_for(file),
    )

    assert response.data.format == "srt"
    assert response.data.event_count == 2
    assert response.data.cues[0].model_dump() == {
        "start_ms": 1250,
        "end_ms": 3000,
        "text": "中文字幕\nChinese subtitle",
    }
    assert response.data.cues[1].text == "第二句对白"


async def test_preview_rejects_untracked_external_filename(tmp_path) -> None:
    file = _file(tmp_path, embedded=[], external=[])

    with pytest.raises(NotFoundException, match="已不存在"):
        await preview_file_subtitle(
            file_id=7,
            track="external:../secret.srt",
            principal=Principal(kind="admin", name="tester"),
            session=_session_for(file),
        )


async def test_preview_load_error_does_not_expose_server_path(tmp_path, caplog) -> None:
    filename = "Movie.missing.srt"
    file = _file(
        tmp_path,
        embedded=[],
        external=[{"filename": filename, "format": "srt", "language": "chi"}],
    )

    with pytest.raises(BadRequestException) as caught:
        await preview_file_subtitle(
            file_id=7,
            track=f"external:{filename}",
            principal=Principal(kind="member", name="tester"),
            session=_session_for(file),
        )

    assert str(tmp_path) not in str(caught.value)
    assert "字幕文件无法读取或解析" in str(caught.value)
    assert str(tmp_path) in caplog.text


async def test_preview_text_embedded_subtitle_uses_stream_index(tmp_path, monkeypatch) -> None:
    file = _file(
        tmp_path,
        embedded=[{"codec": "ass", "language": "eng", "title": "English"}],
        external=[],
    )

    async def fake_load(row, candidate, *, preserve_linebreaks=False):
        assert row is file
        assert (candidate.kind, candidate.key, candidate.format) == ("embedded", "0", "ass")
        assert preserve_linebreaks is True
        return [(500, 1800, "Embedded line")]

    monkeypatch.setattr(extract, "load_candidate_events", fake_load)

    response = await preview_file_subtitle(
        file_id=7,
        track="embedded:0",
        principal=Principal(kind="admin", name="tester"),
        session=_session_for(file),
    )

    assert response.data.event_count == 1
    assert response.data.cues[0].text == "Embedded line"


async def test_preview_graphic_embedded_subtitle_explains_ocr_boundary(tmp_path) -> None:
    file = _file(
        tmp_path,
        embedded=[{"codec": "hdmv_pgs_subtitle", "language": "chi"}],
        external=[],
    )

    with pytest.raises(BadRequestException, match="图形字幕"):
        await preview_file_subtitle(
            file_id=7,
            track="embedded:0",
            principal=Principal(kind="admin", name="tester"),
            session=_session_for(file),
        )
