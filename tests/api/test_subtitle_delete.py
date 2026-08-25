"""删除外挂字幕：归属校验、台账刷新与磁盘边界。

删除是不可撤销的磁盘动作，这里守的是"只能删得掉这个视频的外挂字幕"——
目录穿越、别的影片的字幕、视频本体、内封轨，一律拒绝。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from movieclaw_api.api.routes.libraries import delete_file_subtitle
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_db.models import LibraryFile


def _file(tmp_path) -> LibraryFile:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    return LibraryFile(
        id=7,
        library_id=3,
        media_item_id=9,
        file_path=str(video),
        subtitle_streams=[{"codec": "subrip", "language": "chi"}],
        external_subtitles=None,
        source="scanned",
    )


def _session_for(file: LibraryFile) -> AsyncMock:
    session = AsyncMock()
    session.get.return_value = file
    return session


async def test_delete_removes_file_and_refreshes_ledger(tmp_path) -> None:
    file = _file(tmp_path)
    (tmp_path / "Movie.chs.srt").write_text("1\n", encoding="utf-8")
    kept = tmp_path / "Movie.eng.srt"
    kept.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
    session = _session_for(file)

    response = await delete_file_subtitle(file_id=7, filename="Movie.chs.srt", session=session)

    assert not (tmp_path / "Movie.chs.srt").exists()
    assert kept.exists()
    assert response.data.path == str(tmp_path / "Movie.chs.srt")
    assert response.data.freed_bytes == 2
    # 台账即时刷新：只剩没被删的那一条，详情页不必等下一次扫描
    assert [entry["filename"] for entry in file.external_subtitles or []] == ["Movie.eng.srt"]
    session.commit.assert_awaited()


async def test_delete_ai_generated_sidecar(tmp_path) -> None:
    """AI 字幕就是落在视频同目录的 sidecar，与手工外挂走同一条路径。"""
    file = _file(tmp_path)
    (tmp_path / "Movie.ai-chs.srt").write_text("1\n", encoding="utf-8")

    await delete_file_subtitle(file_id=7, filename="Movie.ai-chs.srt", session=_session_for(file))

    assert not (tmp_path / "Movie.ai-chs.srt").exists()


@pytest.mark.parametrize(
    "filename",
    [
        "../Movie.chs.srt",  # 目录穿越
        "Other.chs.srt",  # 别的影片的字幕
        "Movie.mkv",  # 视频本体（不是字幕扩展名）
        "Movie.nfo",  # 同名附属文件，但不是字幕
    ],
)
async def test_delete_rejects_files_outside_this_video_subtitles(tmp_path, filename: str) -> None:
    file = _file(tmp_path)
    victim = tmp_path / "Movie.nfo"
    victim.write_text("<movie/>", encoding="utf-8")

    with pytest.raises(BadRequestException):
        await delete_file_subtitle(file_id=7, filename=filename, session=_session_for(file))

    assert victim.exists()
    assert (tmp_path / "Movie.mkv").exists()


async def test_delete_reports_missing_file(tmp_path) -> None:
    file = _file(tmp_path)

    with pytest.raises(NotFoundException, match="不在磁盘"):
        await delete_file_subtitle(file_id=7, filename="Movie.chs.srt", session=_session_for(file))


async def test_delete_reports_unknown_library_file() -> None:
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(NotFoundException, match="台账文件不存在"):
        await delete_file_subtitle(file_id=404, filename="Movie.chs.srt", session=session)
